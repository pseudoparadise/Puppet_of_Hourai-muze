"""
console.py — phantom-trigger 统一控制台
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


def _parse_content_parts(content_str: str) -> tuple:
    """解析 '原话：xxx | 概括：yyy' 格式，返回 (raw, summary)。"""
    if not content_str:
        return "", ""
    raw, summary = "", content_str
    if content_str.startswith("原话："):
        parts = content_str.split(" | 概括：", 1)
        raw = parts[0][3:]  # 去掉 '原话：'
        summary = parts[1] if len(parts) > 1 else ""
    elif content_str.startswith("概括："):
        summary = content_str[3:]
    return raw, summary


class DashboardTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._canvas = tk.Canvas(self, highlightthickness=0)
        self._scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self._canvas.yview)
        self._inner = ttk.Frame(self._canvas)
        self._inner.bind("<Configure>", lambda e: self._canvas.configure(
            scrollregion=self._canvas.bbox("all")))
        self._canvas.create_window((0, 0), window=self._inner, anchor="nw", tags="dash_inner")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)
        self._canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self._scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self._canvas.bind("<Enter>", lambda e: self._canvas.bind_all("<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda e: self._canvas.unbind_all("<MouseWheel>"))
        self.bind("<Configure>", lambda e: self._canvas.itemconfig("dash_inner", width=self.winfo_width() - 20))
        self.refresh()

    def _on_wheel(self, event):
        self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def refresh(self):
        for w in self._inner.winfo_children():
            w.destroy()

        try:
            self._build()
        except Exception as e:
            ttk.Label(self._inner, text=f"刷新失败: {e}", foreground="red",
                      font=("", 12, "bold")).pack(pady=40)

    def _build(self):
        # ── 服务控制栏 ──
        svc_frame = ttk.LabelFrame(self._inner, text="服务控制", padding=10)
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
        stats_frame = ttk.LabelFrame(self._inner, text="系统状态", padding=10)
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

        # ── 手动情绪 ──
        emo_frame = ttk.LabelFrame(self._inner, text="手动情绪 (VA先验 — 设了就走我的，没设就走模型)", padding=10)
        emo_frame.pack(fill=tk.X, padx=10, pady=5)
        self.manual_va_path = os.path.join(PROJECT_ROOT, "manual_va.json")
        self._load_manual_va()

        emo_row1 = ttk.Frame(emo_frame)
        emo_row1.pack(fill=tk.X)
        self.va_enabled_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(emo_row1, text="启用手动情绪", variable=self.va_enabled_var,
                        command=self._on_va_toggle).pack(side=tk.LEFT, padx=5)

        self.va_trust_var = tk.DoubleVar(value=0.8)
        ttk.Label(emo_row1, text="信任度:").pack(side=tk.LEFT, padx=(15, 2))
        ttk.Spinbox(emo_row1, from_=0.0, to=1.0, increment=0.05, textvariable=self.va_trust_var,
                    width=5).pack(side=tk.LEFT)

        emo_row2 = ttk.Frame(emo_frame)
        emo_row2.pack(fill=tk.X, pady=2)
        ttk.Label(emo_row2, text="效价 V:", width=8).pack(side=tk.LEFT)
        ttk.Label(emo_row2, text="负(不愉快)", foreground="gray", font=("", 7)).pack(side=tk.LEFT)
        self.va_valence_var = tk.DoubleVar(value=0.0)
        self.va_valence_scale = ttk.Scale(emo_row2, from_=-1.0, to=1.0, variable=self.va_valence_var,
                                          orient=tk.HORIZONTAL, length=250)
        self.va_valence_scale.pack(side=tk.LEFT, padx=5)
        ttk.Label(emo_row2, text="正(愉快)", foreground="gray", font=("", 7)).pack(side=tk.LEFT)
        self.va_v_label = ttk.Label(emo_row2, text="0.00", width=5)
        self.va_v_label.pack(side=tk.LEFT, padx=5)
        self.va_valence_scale.bind("<ButtonRelease-1>", lambda e: self._on_va_slide())
        self.va_valence_scale.bind("<B1-Motion>", lambda e: self._on_va_slide_live())

        emo_row3 = ttk.Frame(emo_frame)
        emo_row3.pack(fill=tk.X, pady=2)
        ttk.Label(emo_row3, text="唤醒 A:", width=8).pack(side=tk.LEFT)
        ttk.Label(emo_row3, text="低(平静)", foreground="gray", font=("", 7)).pack(side=tk.LEFT)
        self.va_arousal_var = tk.DoubleVar(value=0.5)
        self.va_arousal_scale = ttk.Scale(emo_row3, from_=-1.0, to=1.0, variable=self.va_arousal_var,
                                          orient=tk.HORIZONTAL, length=250)
        self.va_arousal_scale.pack(side=tk.LEFT, padx=5)
        ttk.Label(emo_row3, text="高(亢奋)", foreground="gray", font=("", 7)).pack(side=tk.LEFT)
        self.va_a_label = ttk.Label(emo_row3, text="0.50", width=5)
        self.va_a_label.pack(side=tk.LEFT, padx=5)
        self.va_arousal_scale.bind("<ButtonRelease-1>", lambda e: self._on_va_slide())
        self.va_arousal_scale.bind("<B1-Motion>", lambda e: self._on_va_slide_live())

        emo_row4 = ttk.Frame(emo_frame)
        emo_row4.pack(fill=tk.X, pady=(3, 0))
        ttk.Button(emo_row4, text="保存情绪设定", command=self._save_manual_va).pack(side=tk.LEFT, padx=5)
        ttk.Button(emo_row4, text="清除手动情绪", command=self._clear_manual_va).pack(side=tk.LEFT, padx=5)
        self.va_status_label = ttk.Label(emo_row4, text="", foreground="gray")
        self.va_status_label.pack(side=tk.LEFT, padx=10)
        self._update_va_labels()

        # ── Persona 情感状态 ──
        persona_frame = ttk.LabelFrame(self._inner, text="Persona — DS对沐泽的感情", padding=10)
        persona_frame.pack(fill=tk.X, padx=10, pady=5)

        self.persona_affinity_var = tk.DoubleVar(value=0.60)
        self.persona_tenderness_var = tk.DoubleVar(value=0.55)
        try:
            from memory.persona_engine import load_state
            ps = load_state()
            self.persona_affinity_var.set(ps.get("affinity", 0.60))
            self.persona_tenderness_var.set(ps.get("tenderness", 0.55))
        except Exception:
            pass

        p_row1 = ttk.Frame(persona_frame)
        p_row1.pack(fill=tk.X, pady=2)
        ttk.Label(p_row1, text="亲近度:", width=8).pack(side=tk.LEFT)
        self.persona_a_label = ttk.Label(p_row1, text="0.60", width=5)
        self.persona_a_label.pack(side=tk.LEFT, padx=5)

        def _adj_affinity(d):
            v = self.persona_affinity_var.get() + d
            v = max(0.0, min(1.0, v))
            self.persona_affinity_var.set(round(v, 2))
            self.persona_a_label.config(text=f"{v:.2f}")
            try:
                from memory.persona_engine import manual_adjust
                manual_adjust("affinity", d)
            except Exception:
                pass

        ttk.Button(p_row1, text="−0.05", width=5,
                   command=lambda: _adj_affinity(-0.05)).pack(side=tk.LEFT, padx=1)
        ttk.Button(p_row1, text="+0.05", width=5,
                   command=lambda: _adj_affinity(0.05)).pack(side=tk.LEFT, padx=1)
        ttk.Label(p_row1, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=10)

        ttk.Label(p_row1, text="温柔度:", width=8).pack(side=tk.LEFT)
        self.persona_t_label = ttk.Label(p_row1, text="0.55", width=5)
        self.persona_t_label.pack(side=tk.LEFT, padx=5)

        def _adj_tenderness(d):
            v = self.persona_tenderness_var.get() + d
            v = max(0.0, min(1.0, v))
            self.persona_tenderness_var.set(round(v, 2))
            self.persona_t_label.config(text=f"{v:.2f}")
            try:
                from memory.persona_engine import manual_adjust
                manual_adjust("tenderness", d)
            except Exception:
                pass

        ttk.Button(p_row1, text="−0.05", width=5,
                   command=lambda: _adj_tenderness(-0.05)).pack(side=tk.LEFT, padx=1)
        ttk.Button(p_row1, text="+0.05", width=5,
                   command=lambda: _adj_tenderness(0.05)).pack(side=tk.LEFT, padx=1)

        p_row2 = ttk.Frame(persona_frame)
        p_row2.pack(fill=tk.X, pady=(3, 0))
        self.persona_trend_label = ttk.Label(p_row2, text="趋势: —", foreground="gray")
        self.persona_trend_label.pack(side=tk.LEFT, padx=5)
        try:
            from memory.persona_engine import load_state
            ps = load_state()
            trend_map = {"warming": "回暖中", "cooling": "回落中", "stable": "平稳"}
            self.persona_trend_label.config(
                text=f"趋势: {trend_map.get(ps.get('trend_7d', 'stable'), '平稳')}")
        except Exception:
            pass

        # ── 分类分布 ──
        cat_frame = ttk.LabelFrame(self._inner, text="分类分布", padding=10)
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
        diary_frame = ttk.LabelFrame(self._inner, text="最近日记", padding=10)
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
        music_frame = ttk.LabelFrame(self._inner, text="实时音乐", padding=10)
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
        ttk.Button(self._inner, text="刷新", command=self.refresh).pack(pady=5)

    def _load_manual_va(self):
        if os.path.exists(self.manual_va_path):
            try:
                with open(self.manual_va_path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                self.va_enabled_var.set(d.get("enabled", False))
                self.va_valence_var.set(d.get("valence", 0.0))
                self.va_arousal_var.set(d.get("arousal", 0.5))
                self.va_trust_var.set(d.get("trust", 0.8))
            except Exception:
                pass

    def _save_manual_va(self):
        d = {
            "enabled": self.va_enabled_var.get(),
            "valence": round(self.va_valence_var.get(), 2),
            "arousal": round(self.va_arousal_var.get(), 2),
            "trust": round(self.va_trust_var.get(), 2),
        }
        try:
            with open(self.manual_va_path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            self.va_status_label.config(text="已保存", foreground="green")
        except Exception as e:
            self.va_status_label.config(text=f"保存失败: {e}", foreground="red")

    def _clear_manual_va(self):
        self.va_enabled_var.set(False)
        self.va_valence_var.set(0.0)
        self.va_arousal_var.set(0.5)
        self.va_trust_var.set(0.8)
        self._update_va_labels()
        self._save_manual_va()
        self.va_status_label.config(text="已清除，回退模型估测", foreground="gray")

    def _on_va_toggle(self):
        self._save_manual_va()

    def _on_va_slide(self):
        self._update_va_labels()
        self._save_manual_va()

    def _on_va_slide_live(self):
        self._update_va_labels()

    def _update_va_labels(self):
        self.va_v_label.config(text=f"{self.va_valence_var.get():.2f}")
        self.va_a_label.config(text=f"{self.va_arousal_var.get():.2f}")

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


class CardEditTab(ttk.Frame):
    CATEGORIES = [
        "milestone", "commitments", "turning_points", "deep_talks",
        "interaction", "preferences", "real_world", "daily_life",
        "emotional", "habits", "erotic", "todo"
    ]

    def __init__(self, parent):
        super().__init__(parent)
        self._card = None
        self._build()

    def _build(self):
        # ── 顶栏：搜索 ──
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(top, text="卡片ID或标题关键词:").pack(side=tk.LEFT, padx=2)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var, width=40)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<Return>", lambda e: self._search())
        ttk.Button(top, text="搜索", command=self._search).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="刷新", command=self._search).pack(side=tk.LEFT, padx=2)

        # ── 中栏：结果列表 + 编辑区 ──
        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 左侧：搜索结果
        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        columns = ("id", "title", "cat", "imp", "content_preview")
        self.result_tree = ttk.Treeview(left, columns=columns, show="headings", height=18)
        self.result_tree.heading("id", text="ID")
        self.result_tree.heading("title", text="标题")
        self.result_tree.heading("cat", text="分类")
        self.result_tree.heading("imp", text="重要度")
        self.result_tree.heading("content_preview", text="内容预览")
        self.result_tree.column("id", width=100)
        self.result_tree.column("title", width=90)
        self.result_tree.column("cat", width=60)
        self.result_tree.column("imp", width=40)
        self.result_tree.column("content_preview", width=220)
        self.result_tree.pack(fill=tk.BOTH, expand=True)
        self.result_tree.bind("<<TreeviewSelect>>", self._on_select)

        # 右侧：编辑面板
        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        self.edit_frame = right

        self._build_editor(right)

        # ── 底栏 ──
        bottom = ttk.Frame(self)
        bottom.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(bottom, text="保存修改", command=self._save).pack(side=tk.LEFT, padx=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var, foreground="gray").pack(side=tk.LEFT, padx=10)

    def _build_editor(self, parent):
        f = ttk.LabelFrame(parent, text="卡片编辑", padding=10)
        f.pack(fill=tk.BOTH, expand=True)

        row0 = ttk.Frame(f)
        row0.pack(fill=tk.X, pady=2)
        ttk.Label(row0, text="ID:", width=8).pack(side=tk.LEFT)
        self.var_id = tk.StringVar()
        ttk.Entry(row0, textvariable=self.var_id, state="readonly", width=50).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row1 = ttk.Frame(f)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="标题:", width=8).pack(side=tk.LEFT)
        self.var_title = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_title, width=50).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row1b = ttk.Frame(f)
        row1b.pack(fill=tk.X, pady=2)
        ttk.Label(row1b, text="分类:", width=8).pack(side=tk.LEFT)
        self.var_category = tk.StringVar(value="interaction")
        cb = ttk.Combobox(row1b, textvariable=self.var_category, values=self.CATEGORIES, state="readonly", width=18)
        cb.pack(side=tk.LEFT, padx=2)
        ttk.Label(row1b, text="重要度(1-10):", width=12).pack(side=tk.LEFT, padx=(20, 2))
        self.var_importance = tk.IntVar(value=5)
        ttk.Spinbox(row1b, from_=1, to=10, textvariable=self.var_importance, width=4).pack(side=tk.LEFT)

        row1c = ttk.Frame(f)
        row1c.pack(fill=tk.X, pady=2)
        ttk.Label(row1c, text="效价:", width=8).pack(side=tk.LEFT)
        self.var_valence = tk.DoubleVar(value=0.0)
        ttk.Spinbox(row1c, from_=-1.0, to=1.0, increment=0.05, textvariable=self.var_valence, width=6).pack(side=tk.LEFT)
        ttk.Label(row1c, text="唤醒:", width=6).pack(side=tk.LEFT, padx=(10, 2))
        self.var_arousal = tk.DoubleVar(value=0.5)
        ttk.Spinbox(row1c, from_=-1.0, to=1.0, increment=0.05, textvariable=self.var_arousal, width=6).pack(side=tk.LEFT)
        ttk.Label(row1c, text="和弦:", width=6).pack(side=tk.LEFT, padx=(10, 2))
        self.var_chord = tk.StringVar()
        ttk.Entry(row1c, textvariable=self.var_chord, width=22).pack(side=tk.LEFT)

        chk_row = ttk.Frame(f)
        chk_row.pack(fill=tk.X, pady=2)
        self.var_enabled = tk.BooleanVar(value=True)
        ttk.Checkbutton(chk_row, text="活跃(enabled_in_context)", variable=self.var_enabled).pack(side=tk.LEFT, padx=2)
        self.var_resolved = tk.BooleanVar(value=False)
        ttk.Checkbutton(chk_row, text="已解决(resolved)", variable=self.var_resolved).pack(side=tk.LEFT, padx=10)

        row2 = ttk.Frame(f)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="关键词:", width=8).pack(side=tk.LEFT)
        self.var_keywords = tk.StringVar()
        ttk.Entry(row2, textvariable=self.var_keywords, width=50).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row3 = ttk.Frame(f)
        row3.pack(fill=tk.BOTH, expand=True, pady=2)
        ttk.Label(row3, text="标志性原话:", width=10).pack(side=tk.LEFT, anchor=tk.N)
        self.raw_text = tk.Text(row3, height=3, wrap=tk.WORD)
        self.raw_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        raw_scroll = ttk.Scrollbar(row3, command=self.raw_text.yview)
        raw_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.raw_text.config(yscrollcommand=raw_scroll.set)

        row4 = ttk.Frame(f)
        row4.pack(fill=tk.BOTH, expand=True, pady=2)
        ttk.Label(row4, text="事件概括:", width=10).pack(side=tk.LEFT, anchor=tk.N)
        self.summary_text = tk.Text(row4, height=4, wrap=tk.WORD)
        self.summary_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sum_scroll = ttk.Scrollbar(row4, command=self.summary_text.yview)
        sum_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.summary_text.config(yscrollcommand=sum_scroll.set)

    def _search(self):
        query = self.search_var.get().strip()
        for item in self.result_tree.get_children():
            self.result_tree.delete(item)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        if query:
            c.execute(
                "SELECT id, title, category, importance, content FROM cards WHERE review_status='final' "
                "AND (id LIKE ? OR title LIKE ?) ORDER BY importance DESC LIMIT 50",
                (f"%{query}%", f"%{query}%")
            )
        else:
            c.execute(
                "SELECT id, title, category, importance, content FROM cards WHERE review_status='final' "
                "ORDER BY importance DESC LIMIT 50"
            )
        for r in c.fetchall():
            preview = (r["content"] or "")[:60]
            self.result_tree.insert("", tk.END,
                values=(r["id"], r["title"], r["category"], r["importance"], preview),
                iid=r["id"])
        conn.close()
        self.status_var.set(f"找到 {len(self.result_tree.get_children())} 张卡片")

    def _on_select(self, event):
        try:
            sel = self.result_tree.selection()
            if not sel:
                return
            card_id = sel[0]

            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM cards WHERE id=?", (card_id,))
            row = c.fetchone()
            if not row:
                conn.close()
                self.status_var.set(f"卡片 {card_id} 不在库中")
                return
            self._card = dict(row)
            conn.close()
        except Exception as _e:
            import traceback as _tb
            _tb.print_exc()
            messagebox.showerror("卡片编辑-加载异常", f"{type(_e).__name__}: {_e}")
            self.status_var.set(f"加载失败: {_e}")
            return
        try:
            self.var_id.set(self._card.get("id", ""))
            self.var_title.set(self._card.get("title", ""))
            self.var_category.set(self._card.get("category", "interaction"))
            self.var_importance.set(self._card.get("importance", 5))
            self.var_valence.set(self._card.get("valence", 0.0) or 0.0)
            self.var_arousal.set(self._card.get("arousal", 0.5) or 0.5)
            self.var_chord.set(self._card.get("chord", "") or "")
            self.var_enabled.set(bool(self._card.get("enabled_in_context", 1)))
            self.var_resolved.set(bool(self._card.get("resolved", 0)))
            self.var_keywords.set(self._card.get("keywords", "") or "")
            content_str = self._card.get("content", "") or ""
            raw_part, summary_part = _parse_content_parts(content_str)
            self.raw_text.delete("1.0", tk.END)
            self.raw_text.insert("1.0", raw_part)
            self.summary_text.delete("1.0", tk.END)
            self.summary_text.insert("1.0", summary_part)
            self.status_var.set(f"已加载: {card_id}")
        except Exception as _e:
            import traceback as _tb
            _tb.print_exc()
            messagebox.showerror("卡片编辑-字段加载异常", f"{type(_e).__name__}: {_e}")
            self.status_var.set(f"加载失败: {_e}")

    def _save(self):
        if not self._card:
            self.status_var.set("请先搜索并选中一张卡片")
            return
        card_id = self._card["id"]

        title = self.var_title.get().strip()
        raw_quote = self.raw_text.get("1.0", tk.END).strip()
        summary = self.summary_text.get("1.0", tk.END).strip()
        content = f"原话：{raw_quote} | 概括：{summary}" if raw_quote else f"概括：{summary}"
        keywords = self.var_keywords.get().strip()
        category = self.var_category.get()
        importance = self.var_importance.get()
        valence = self.var_valence.get()
        arousal = self.var_arousal.get()
        chord = self.var_chord.get().strip()
        enabled = 1 if self.var_enabled.get() else 0
        resolved = 1 if self.var_resolved.get() else 0

        if not title:
            self.status_var.set("标题不能为空")
            return

        if not messagebox.askyesno("确认保存", f"确定保存对卡片「{title}」的修改吗？\n\n此操作直接写入数据库。"):
            return

        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute(
                "UPDATE cards SET title=?, content=?, keywords=?, importance=?, category=?, "
                "valence=?, arousal=?, chord=?, enabled_in_context=?, resolved=? WHERE id=?",
                (title, content, keywords, importance, category, valence, arousal, chord, enabled, resolved, card_id)
            )
            if c.rowcount == 0:
                messagebox.showerror("失败", "未找到对应卡片。")
                conn.close()
                return
            conn.commit()
            # 内容或keywords改了 → 重算 embedding
            if content != (self._card.get("content") or "") or keywords != (self._card.get("keywords") or ""):
                try:
                    self._re_embed(card_id, title, content, keywords)
                except Exception as e:
                    import traceback as _tb_re
                    _tb_re.print_exc()
                    messagebox.showerror("卡片编辑-re-embed异常", f"{type(e).__name__}: {e}")
            self._card = {**self._card, "title": title, "content": content, "keywords": keywords,
                          "category": category, "importance": importance, "valence": valence,
                          "arousal": arousal, "chord": chord, "enabled_in_context": enabled, "resolved": resolved}
            self.status_var.set(f"已保存: {card_id}")
            messagebox.showinfo("成功", f"卡片「{title}」已保存。")
            self._search()
        except Exception as e:
            import traceback as _tb_s
            _tb_s.print_exc()
            messagebox.showerror("卡片编辑-保存异常", f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    def _re_embed(self, card_id, title, content, keywords):
        from memory.encoder import embed, load_index, add_to_index, save_index, remove_from_index, build_embed_text
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        card = dict(row) if row else {"title": title, "content": content, "keywords": keywords, "user_raw": "", "category": ""}
        conn.close()
        vec = embed(build_embed_text(card))
        vec_bytes = vec.tobytes()
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE cards SET embedding=? WHERE id=?", (vec_bytes, card_id))
        conn.commit()
        conn.close()
        remove_from_index(card_id)
        index = load_index()
        add_to_index(index, card_id, vec)
        save_index(index)
        print(f"[editor] embedding 已更新: {card_id}")


class RecallFeedbackTab(ttk.Frame):
    TRACE_PATH = os.path.join(PROJECT_ROOT, "memory", "retrieval_traces.jsonl")

    def __init__(self, parent):
        super().__init__(parent)
        self._build()

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=5)
        ttk.Button(top, text="刷新列表", command=self._load).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="应用反馈微调", command=self._apply).pack(side=tk.LEFT, padx=5)
        self.auto_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="5轮自动微调", variable=self.auto_var).pack(side=tk.LEFT, padx=10)
        self.stats_label = ttk.Label(top, text="", foreground="gray")
        self.stats_label.pack(side=tk.LEFT, padx=15)

        # 左右分栏
        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 左侧：检索轮次列表
        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        self.tree = ttk.Treeview(left, columns=("ts", "query", "va", "count"), show="headings", height=20)
        self.tree.heading("ts", text="时间")
        self.tree.heading("query", text="查询")
        self.tree.heading("va", text="VA档")
        self.tree.heading("count", text="卡片数")
        self.tree.column("ts", width=100)
        self.tree.column("query", width=130)
        self.tree.column("va", width=45)
        self.tree.column("count", width=45)
        self.tree.pack(fill=tk.BOTH, expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # 右侧：选中轮次的卡片详情
        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        columns = ("title", "cat", "score", "kw", "sem", "imp", "anc", "dif", "rec", "dec", "fir", "wat", "gro", "fb")
        self.card_tree = ttk.Treeview(right, columns=columns, show="headings", height=18)
        self.card_tree.heading("title", text="标题")
        self.card_tree.heading("cat", text="分类")
        self.card_tree.heading("score", text="总分")
        self.card_tree.heading("kw", text="词")
        self.card_tree.heading("sem", text="语")
        self.card_tree.heading("imp", text="重")
        self.card_tree.heading("anc", text="锚")
        self.card_tree.heading("dif", text="风")
        self.card_tree.heading("rec", text="雷")
        self.card_tree.heading("dec", text="冰")
        self.card_tree.heading("fir", text="火")
        self.card_tree.heading("wat", text="水")
        self.card_tree.heading("gro", text="草")
        self.card_tree.heading("fb", text="反馈")
        for col in columns:
            self.card_tree.column(col, width=45 if col not in ("title", "fb") else (80 if col == "title" else 50))
        self.card_tree.pack(fill=tk.BOTH, expand=True)

        # 底栏按钮
        bottom = ttk.Frame(right)
        bottom.pack(fill=tk.X, pady=5)
        ttk.Button(bottom, text="✓ 准确", command=lambda: self._mark("good")).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="✗ 不准", command=lambda: self._mark("bad")).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="清除标记", command=lambda: self._mark(None)).pack(side=tk.LEFT, padx=5)
        self.fb_status = ttk.Label(bottom, text="", foreground="gray")
        self.fb_status.pack(side=tk.LEFT, padx=10)

        # QA 上下文面板
        qa_frame = ttk.LabelFrame(right, text="对话上下文 (chat_logs.json)", padding=5)
        qa_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.qa_text = tk.Text(qa_frame, height=6, wrap=tk.WORD, font=("", 9))
        self.qa_text.pack(fill=tk.BOTH, expand=True)
        self.qa_text.config(state=tk.DISABLED)

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
        with open(self.TRACE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    t = json.loads(line.strip())
                    tid = t["trace_id"]
                    self._traces[tid] = t
                    cards = t.get("cards", [])
                    for c in cards:
                        if c.get("feedback") == "good":
                            good += 1
                        elif c.get("feedback") == "bad":
                            bad += 1
                    self.tree.insert("", 0, values=(
                        t.get("ts", "")[:16],
                        t.get("query", "")[:50],
                        t.get("va_tier", "?"),
                        len(cards)
                    ), iid=tid)
                except Exception:
                    pass
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
                c.get("title", "")[:20],
                c.get("category", ""),
                f"{c.get('score', 0):.2f}",
                f"{p.get('keyword', 0):.2f}",
                f"{p.get('semantic', 0):.2f}",
                f"{p.get('importance', 0):.2f}",
                f"{p.get('anchor', 0):.2f}",
                f"{p.get('diffusion', 0):.2f}",
                f"{p.get('recency', 0):.2f}",
                f"{p.get('decay', 0):.2f}",
                f"{p.get('fire', 0):.2f}",
                f"{p.get('water', 0):.2f}",
                f"{p.get('growth', 0):.2f}",
                fb_str
            ), iid=c["id"])

        # 加载 QA 上下文
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
        label = "✓" if fb_val == "good" else ("✗" if fb_val == "bad" else "○")

        # 自动微调计数器
        if not hasattr(self, '_auto_count'):
            self._auto_count = 0
        self._auto_count += 1
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
        """根据 trace 的查询和时间戳，从 chat_logs.json 拉出对应对话。"""
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
                    e = json.loads(line.strip())
                    entries.append(e)
                except json.JSONDecodeError:
                    pass

        # 找最接近 trace 时间戳的 user 消息
        best_idx = -1
        best_score = 999
        for i, e in enumerate(entries):
            ets = e.get("timestamp", "")
            if query and query[:30] in str(e.get("content", "")):
                best_idx = i
                break
            # 备选：时间戳最接近
            if ts and ets:
                try:
                    dist = abs(len(ets) - len(ts))
                    if dist < best_score:
                        best_score = dist
                        best_idx = i
                except:
                    pass

        if best_idx < 0:
            self.qa_text.insert(tk.END, f"(未找到匹配对话)\n查询: {query}")
            self.qa_text.config(state=tk.DISABLED)
            return

        # 显示前后 3 条
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
        if not messagebox.askyesno("确认", "根据所有已标记反馈微调探针权重？\n\n✓ 卡片增强其高分探针\n✗ 卡片削弱其高分探针"):
            return
        from memory.retriever import apply_feedback_adjustments, get_effective_weights
        result = apply_feedback_adjustments()
        eff = get_effective_weights()
        msg = f"✓{result['good']}张 ✗{result['bad']}张\n\n当前有效权重:\n"
        for k in ["w_keyword", "w_semantic", "w_importance", "w_anchor",
                   "w_diffusion", "w_recency", "w_fire", "w_water"]:
            msg += f"  {k}: {eff.get(k, '?')}\n"
        messagebox.showinfo("微调完成", msg)
        self._load()


class HumanWriteCardTab(ttk.Frame):
    CATEGORIES = [
        "deep_talks", "milestone", "turning_points", "commitments",
        "todo", "daily_life", "emotional", "interaction", "preferences",
        "erotic", "real_world"
    ]

    def __init__(self, parent):
        super().__init__(parent)
        self._build()

    def _build(self):
        f = ttk.LabelFrame(self, text="人类写卡 — 给不写卡的AI兜底", padding=10)
        f.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        row1 = ttk.Frame(f)
        row1.pack(fill=tk.X, pady=2)
        ttk.Label(row1, text="标题:", width=8).pack(side=tk.LEFT)
        self.var_title = tk.StringVar()
        ttk.Entry(row1, textvariable=self.var_title, width=60).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row1b = ttk.Frame(f)
        row1b.pack(fill=tk.X, pady=2)
        ttk.Label(row1b, text="分类:", width=8).pack(side=tk.LEFT)
        self.var_category = tk.StringVar(value="deep_talks")
        ttk.Combobox(row1b, textvariable=self.var_category, values=self.CATEGORIES,
                     state="readonly", width=18).pack(side=tk.LEFT, padx=2)
        ttk.Label(row1b, text="重要度(1-10):", width=12).pack(side=tk.LEFT, padx=(20, 2))
        self.var_importance = tk.IntVar(value=6)
        ttk.Spinbox(row1b, from_=1, to=10, textvariable=self.var_importance, width=4).pack(side=tk.LEFT)
        ttk.Label(row1b, text="效价:", width=6).pack(side=tk.LEFT, padx=(10, 2))
        self.var_valence = tk.DoubleVar(value=0.0)
        ttk.Spinbox(row1b, from_=-1.0, to=1.0, increment=0.05, textvariable=self.var_valence, width=6).pack(side=tk.LEFT)
        ttk.Label(row1b, text="唤醒:", width=6).pack(side=tk.LEFT, padx=(5, 2))
        self.var_arousal = tk.DoubleVar(value=0.5)
        ttk.Spinbox(row1b, from_=-1.0, to=1.0, increment=0.05, textvariable=self.var_arousal, width=6).pack(side=tk.LEFT)

        row2 = ttk.Frame(f)
        row2.pack(fill=tk.X, pady=2)
        ttk.Label(row2, text="关键词:", width=8).pack(side=tk.LEFT)
        self.var_keywords = tk.StringVar()
        ttk.Entry(row2, textvariable=self.var_keywords, width=60).pack(side=tk.LEFT, fill=tk.X, expand=True)

        row3 = ttk.Frame(f)
        row3.pack(fill=tk.BOTH, expand=True, pady=2)
        ttk.Label(row3, text="标志性原话:", width=10).pack(side=tk.LEFT, anchor=tk.N)
        self.raw_text = tk.Text(row3, height=3, wrap=tk.WORD)
        self.raw_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        row3b = ttk.Frame(f)
        row3b.pack(fill=tk.BOTH, expand=True, pady=2)
        ttk.Label(row3b, text="事件概括:", width=10).pack(side=tk.LEFT, anchor=tk.N)
        self.summary_text = tk.Text(row3b, height=4, wrap=tk.WORD)
        self.summary_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        row_chord = ttk.Frame(f)
        row_chord.pack(fill=tk.X, pady=2)
        ttk.Label(row_chord, text="和弦标注:", width=10).pack(side=tk.LEFT)
        self.var_chord = tk.StringVar()
        ttk.Entry(row_chord, textvariable=self.var_chord, width=30).pack(side=tk.LEFT)
        ttk.Label(row_chord, text="(可选, 如 Em7Fmaj7.40bpm.pp)", foreground="gray", font=("", 8)).pack(side=tk.LEFT, padx=5)

        row_target = ttk.Frame(f)
        row_target.pack(fill=tk.X, pady=2)
        ttk.Label(row_target, text="目标日期:", width=8).pack(side=tk.LEFT)
        self.var_target = tk.StringVar()
        ttk.Entry(row_target, textvariable=self.var_target, width=25).pack(side=tk.LEFT)
        ttk.Label(row_target, text="(可选, YYYY-MM-DD 或 YYYY-MM-DD HH:MM)",
                  foreground="gray", font=("", 8)).pack(side=tk.LEFT, padx=5)

        bottom = ttk.Frame(f)
        bottom.pack(fill=tk.X, pady=5)
        ttk.Button(bottom, text="写入待审核池", command=self._submit).pack(side=tk.LEFT, padx=5)
        ttk.Button(bottom, text="清空表单", command=self._clear).pack(side=tk.LEFT, padx=5)
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(bottom, textvariable=self.status_var, foreground="gray").pack(side=tk.LEFT, padx=10)

    def _submit(self):
        title = self.var_title.get().strip()
        raw_quote = self.raw_text.get("1.0", tk.END).strip()
        summary = self.summary_text.get("1.0", tk.END).strip()
        content = f"原话：{raw_quote} | 概括：{summary}" if raw_quote else f"概括：{summary}"
        keywords = self.var_keywords.get().strip()
        category = self.var_category.get()
        importance = self.var_importance.get()
        valence = self.var_valence.get()
        arousal = self.var_arousal.get()
        chord = self.var_chord.get().strip()
        target = self.var_target.get().strip()

        if not title:
            self.status_var.set("标题不能为空")
            return
        if not summary:
            self.status_var.set("事件概括不能为空")
            return
        if not keywords:
            self.status_var.set("关键词不能为空")
            return

        card_id = f"{datetime.now().strftime('%Y%m%d')}_{title}"
        card = {
            "id": card_id,
            "title": title,
            "content": content,
            "keywords": keywords,
            "user_raw": raw_quote,
            "category": category,
            "importance": importance,
            "proposed_by": "muze",
            "proposed_at": datetime.now(timezone.utc).isoformat(),
            "review_status": "pending",
            "chord": chord,
            "valence": valence,
            "arousal": arousal,
            "target_date": target or None,
            "time_anchor": {"date": None, "fuzzy": None, "label": None, "days_until": None}
        }

        # 预计算 embedding 供后续去重
        try:
            from memory.encoder import build_embed_text, embed
            card["_embed_vec"] = embed(build_embed_text(card)).tolist()
        except Exception:
            pass

        # ── 重复卡片检查 ──
        pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
        pending = []
        if os.path.exists(pending_path):
            try:
                with open(pending_path, "r", encoding="utf-8") as f:
                    pending = json.load(f)
            except Exception:
                pass
        # 检查 pending 中是否有相似卡片
        _dup_info = None
        for _pc in pending:
            if _pc.get("title", "") == title or content[:30] in _pc.get("content", ""):
                _dup_info = f"待审核池: {_pc.get('title', '')}"
                break
        # 检查 DB 中是否有相似卡片
        if not _dup_info:
            try:
                _db_conn = sqlite3.connect(DB_PATH)
                _db_c = _db_conn.cursor()
                _db_c.execute("SELECT id, title FROM cards WHERE title=? LIMIT 1", (title,))
                _db_row = _db_c.fetchone()
                if _db_row:
                    _dup_info = f"卡片库: {_db_row[1]}"
                _db_conn.close()
            except Exception:
                pass
        if _dup_info:
            if not messagebox.askyesno("重复卡片确认",
                f"检测到相似卡片:\n{_dup_info}\n\n"
                f"即将写入:\n标题: {title}\n内容: {content[:80]}\n\n"
                f"是否保留两张完全一样的卡片？"):
                self.status_var.set("已取消（重复卡片）")
                return

        pending.append(card)
        try:
            from delegate_tools import atomic_write_json
            atomic_write_json(pending_path, pending)
            print(f"[人类写卡] id={card_id} title={title} cat={category} imp={importance} kw={keywords}")
            self.status_var.set(f"已写入: {card_id} (embed 已预计算)")
            self._clear()
        except Exception as e:
            self.status_var.set(f"写入失败: {e}")

    def _clear(self):
        self.var_title.set("")
        self.var_keywords.set("")
        self.var_chord.set("")
        self.var_target.set("")
        self.raw_text.delete("1.0", tk.END)
        self.summary_text.delete("1.0", tk.END)
        self.var_importance.set(6)
        self.var_valence.set(0.0)
        self.var_arousal.set(0.5)
        self.var_category.set("deep_talks")


class Console:
    def __init__(self, root):
        self.root = root
        self.root.title("phantom-trigger 控制台")
        self.root.geometry("1100x800")
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

        # Tab 6: 卡片编辑器
        self.edit_frame = ttk.Frame(notebook)
        notebook.add(self.edit_frame, text="卡片编辑")
        self.edit_tab = CardEditTab(self.edit_frame)
        self.edit_tab.pack(fill=tk.BOTH, expand=True)

        # Tab 7: 召回反馈
        self.feedback_frame = ttk.Frame(notebook)
        notebook.add(self.feedback_frame, text="召回反馈")
        self.feedback_tab = RecallFeedbackTab(self.feedback_frame)
        self.feedback_tab.pack(fill=tk.BOTH, expand=True)

        # Tab 8: 人类写卡
        self.write_frame = ttk.Frame(notebook)
        notebook.add(self.write_frame, text="人类写卡")
        self.write_tab = HumanWriteCardTab(self.write_frame)
        self.write_tab.pack(fill=tk.BOTH, expand=True)


if __name__ == "__main__":
    import traceback as _tb_global

    root = tk.Tk()

    def _tk_error_handler(exc_type, exc_val, exc_tb):
        msg = "".join(_tb_global.format_exception(exc_type, exc_val, exc_tb))
        print(f"[console CRASH] {msg}", file=sys.stderr)
        try:
            with open(os.path.join(PROJECT_ROOT, "console_crash.log"), "a", encoding="utf-8") as _cf:
                _cf.write(f"\n=== {datetime.now().isoformat()} ===\n{msg}\n")
        except Exception:
            pass
        messagebox.showerror("console 异常", f"{exc_type.__name__}: {exc_val}\n\n详情已写入 console_crash.log")

    root.report_callback_exception = _tk_error_handler

    app = Console(root)
    root.mainloop()
