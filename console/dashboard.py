import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import tkinter as tk
from tkinter import ttk
from tkinter import messagebox
from datetime import datetime, timedelta, timezone

from ds_log import info as _log_info
from state.readers import get_daemon_health, get_music_toggle as _read_music_toggle
from . import PROJECT_ROOT, DB_PATH


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
        self._music_proc = None
        self.auto_refresh_var = tk.BooleanVar(value=False)

        from boot_guard import write_pid_with_boot_token
        write_pid_with_boot_token(os.path.join(PROJECT_ROOT, ".console.pid"), os.getpid())

        self.after(10, self.refresh)

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
        svc_frame = ttk.LabelFrame(self._inner, text="服务控制", padding=10)
        svc_frame.pack(fill=tk.X, padx=10, pady=5)
        row_svc = ttk.Frame(svc_frame)
        row_svc.pack(fill=tk.X)

        daemon_pid = self._boot_clean_pid(".daemon.pid")
        daemon_alive = self._pid_alive(daemon_pid)
        daemon_label = f"守护进程: PID {daemon_pid}" if daemon_alive else "守护进程: 未启动"
        ttk.Label(row_svc, text=daemon_label,
                  foreground="green" if daemon_alive else "red").pack(side=tk.LEFT, padx=5)
        ttk.Button(row_svc, text="重启守护", command=self._restart_daemon).pack(side=tk.LEFT, padx=2)

        ttk.Label(row_svc, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=10)
        music_alive = self._music_proc is not None and self._music_proc.poll() is None
        if not music_alive:
            music_alive = self._file_recently_updated(".music_state.json", 30)
        self._music_poll_btn = ttk.Button(row_svc, text="🎧 音乐轮询: 关" if music_alive else "🎧 音乐轮询: 开",
                                         command=self._toggle_music_poll, width=14)
        self._music_poll_btn.pack(side=tk.LEFT, padx=5)

        ttk.Label(row_svc, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=10)
        ttk.Checkbutton(row_svc, text="30s自动刷新", variable=self.auto_refresh_var,
                        command=self._toggle_auto).pack(side=tk.LEFT, padx=5)

        ds = get_daemon_health()
        self._build_daemon_status(svc_frame, ds)

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

        emo_frame = ttk.LabelFrame(self._inner, text="手动情绪 (VA先验)", padding=10)
        emo_frame.pack(fill=tk.X, padx=10, pady=5)
        self.manual_va_path = os.path.join(PROJECT_ROOT, "manual_va.json")

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

        emo_row_mixed = ttk.Frame(emo_frame)
        emo_row_mixed.pack(fill=tk.X, pady=2)
        self.mixed_state_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(emo_row_mixed, text="混合态 [月结晶]（水+岩联动）",
                        variable=self.mixed_state_var, command=self._on_mixed_toggle).pack(side=tk.LEFT, padx=5)
        ttk.Label(emo_row_mixed, text="连续3张高imp锚定卡命中→触发月笼协奏迸发",
                  foreground="#8888cc", font=("", 8)).pack(side=tk.LEFT, padx=10)

        emo_row_cc = ttk.Frame(emo_frame)
        emo_row_cc.pack(fill=tk.X, pady=2)
        self.claude_va_override_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(emo_row_cc, text="Claude Code → 用手动VA",
                        variable=self.claude_va_override_var,
                        command=self._save_manual_va).pack(side=tk.LEFT, padx=5)

        emo_row4 = ttk.Frame(emo_frame)
        emo_row4.pack(fill=tk.X, pady=(3, 0))
        ttk.Button(emo_row4, text="保存情绪设定", command=self._save_manual_va).pack(side=tk.LEFT, padx=5)
        ttk.Button(emo_row4, text="清除手动情绪", command=self._clear_manual_va).pack(side=tk.LEFT, padx=5)
        self.va_status_label = ttk.Label(emo_row4, text="", foreground="gray")
        self.va_status_label.pack(side=tk.LEFT, padx=10)
        self._load_manual_va()
        self._update_va_labels()

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

        ttk.Button(p_row1, text="−0.05", width=5, command=lambda: _adj_affinity(-0.05)).pack(side=tk.LEFT, padx=1)
        ttk.Button(p_row1, text="+0.05", width=5, command=lambda: _adj_affinity(0.05)).pack(side=tk.LEFT, padx=1)
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

        ttk.Button(p_row1, text="−0.05", width=5, command=lambda: _adj_tenderness(-0.05)).pack(side=tk.LEFT, padx=1)
        ttk.Button(p_row1, text="+0.05", width=5, command=lambda: _adj_tenderness(0.05)).pack(side=tk.LEFT, padx=1)

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

        refl_frame = ttk.LabelFrame(self._inner, text="DS 每周自省", padding=10)
        refl_frame.pack(fill=tk.X, padx=10, pady=5)

        refl_row = ttk.Frame(refl_frame)
        refl_row.pack(fill=tk.X)
        self.refl_status_label = ttk.Label(refl_row, text="", foreground="gray")
        self.refl_status_label.pack(side=tk.LEFT, padx=5)
        self.refl_btn_frame = ttk.Frame(refl_row)
        self.refl_btn_frame.pack(side=tk.RIGHT, padx=5)

        def _load_refl_status():
            try:
                from memory.reflection_engine import get_pending_reflection
                pending = get_pending_reflection()
                if pending:
                    self.refl_status_label.config(
                        text=f"待审阅: {pending['week']} ({len(pending['text'])}字)", foreground="orange")
                    for w in self.refl_btn_frame.winfo_children():
                        w.destroy()
                    ttk.Button(self.refl_btn_frame, text="审阅",
                               command=lambda: _show_refl_editor(pending)).pack(side=tk.LEFT, padx=2)
                else:
                    self.refl_status_label.config(text="无待审自省（周日自动生成）", foreground="gray")
                    for w in self.refl_btn_frame.winfo_children():
                        w.destroy()
            except Exception:
                self.refl_status_label.config(text="(reflection_engine 未就绪)", foreground="gray")

        def _show_refl_editor(pending):
            dialog = tk.Toplevel(self)
            dialog.title(f"DS 每周自省 — {pending['week']}")
            dialog.geometry("600x450")
            dialog.resizable(True, True)

            ttk.Label(dialog, text=f"自省报告 — {pending['week']}（可编辑后确认注入 prompt_v1.txt）",
                      font=("", 10, "bold")).pack(pady=10, padx=10)

            editor = tk.Text(dialog, height=15, wrap=tk.WORD, font=("", 10))
            editor.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
            editor.insert("1.0", pending['text'])

            btn_row = ttk.Frame(dialog)
            btn_row.pack(fill=tk.X, pady=10, padx=10)

            def _do_inject():
                new_text = editor.get("1.0", tk.END).strip()
                if not new_text:
                    messagebox.showwarning("内容为空", "自省内容不能为空。")
                    return
                if not messagebox.askyesno("确认注入",
                    "确定将这段自省注入 prompt_v1.txt 吗？"):
                    return
                try:
                    from memory.reflection_engine import inject_to_base_prompt
                    inject_to_base_prompt(new_text)
                    messagebox.showinfo("成功", "自省已注入 prompt_v1.txt")
                    dialog.destroy()
                    _load_refl_status()
                except Exception as e:
                    import traceback; traceback.print_exc()
                    messagebox.showerror("注入失败", f"{e}")

            def _do_save():
                new_text = editor.get("1.0", tk.END).strip()
                if not new_text:
                    messagebox.showwarning("内容为空", "自省内容不能为空。")
                    return
                try:
                    from memory.reflection_engine import save_reflection_edit
                    save_reflection_edit(new_text, pending['week'])
                    messagebox.showinfo("已保存", "自省编辑已保存（未注入 prompt）。")
                except Exception as e:
                    messagebox.showerror("保存失败", f"{e}")

            def _do_discard():
                if messagebox.askyesno("确认丢弃", "确定丢弃本周自省吗？"):
                    try:
                        from memory.reflection_engine import discard_reflection
                        discard_reflection(pending['week'])
                    except Exception:
                        pass
                    dialog.destroy()
                    _load_refl_status()

            ttk.Button(btn_row, text="保存修改", command=_do_save).pack(side=tk.LEFT, padx=5)
            ttk.Button(btn_row, text="注入 prompt", command=_do_inject).pack(side=tk.LEFT, padx=5)
            ttk.Button(btn_row, text="丢弃", command=_do_discard).pack(side=tk.LEFT, padx=5)

        _load_refl_status()

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
            marker = " <- 今天" if d_offset == 0 else (" <- 昨天" if d_offset == 1 else "")
            text.insert(tk.END, f"{d}: {', '.join(status_parts) or '无'}{marker}\n")
        text.config(state=tk.DISABLED)

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

        row_m = ttk.Frame(music_frame)
        row_m.pack(fill=tk.X)
        music_stale = False
        try:
            started_raw = ms.get("started_at", "") if ms else ""
            if started_raw:
                try:
                    start_dt = datetime.fromisoformat(started_raw)
                    if start_dt.tzinfo is None:
                        start_dt = start_dt.replace(tzinfo=timezone.utc)
                    if (datetime.now(timezone.utc) - start_dt).total_seconds() > 7200:
                        music_stale = True
                except Exception:
                    pass

            if ms and ms.get("playing") and ms.get("song_name") and not music_stale:
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

        music_enabled = _read_music_toggle()

        def _toggle_music_btn():
            nonlocal music_enabled
            music_enabled = not music_enabled
            toggle_path = os.path.join(PROJECT_ROOT, ".music_toggle.json")
            try:
                from delegate_tools import atomic_write_json
                atomic_write_json(toggle_path, {"enabled": music_enabled})
            except Exception:
                pass
            self._music_toggle_btn.config(
                text="📝 歌词注入: 开" if music_enabled else "📝 歌词注入: 关",
                foreground="green" if music_enabled else "gray"
            )

        btn_text = "📝 歌词注入: 开" if music_enabled else "📝 歌词注入: 关"
        btn_fg = "green" if music_enabled else "gray"
        self._music_toggle_btn = tk.Button(row_m, text=btn_text, command=_toggle_music_btn)
        self._music_toggle_btn.pack(side=tk.RIGHT, padx=5)

        lyrics_frame = ttk.Frame(music_frame)
        lyrics_frame.pack(fill=tk.X, pady=(5, 0))
        lyrics_text = tk.Text(lyrics_frame, height=4, wrap=tk.WORD)
        lyrics_text.pack(fill=tk.X)
        window = []
        try:
            if ms and ms.get("lyrics") and not music_stale:
                started = ms.get("started_at", "")
                elapsed = 0
                if started:
                    try:
                        start_dt = datetime.fromisoformat(started)
                        if start_dt.tzinfo is None:
                            start_dt = start_dt.replace(tzinfo=timezone.utc)
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
            else:
                lyrics_text.insert(tk.END, "(等待桌面客户端...)" if not music_stale else "(上次播放已过期)")
        except Exception:
            lyrics_text.insert(tk.END, "(歌词加载失败)")
        lyrics_text.config(state=tk.DISABLED)

        music_ctx_path = os.path.join(PROJECT_ROOT, ".music_context.txt")
        if music_enabled and ms and ms.get("playing") and ms.get("song_name") and not music_stale:
            try:
                lines = ["【此刻她正在听的音乐 — 仅当前一首，你可以自然提及，但不要刻意，不要编造其他歌曲】"]
                song = ms.get("song_name", "未知").strip()
                artist = ms.get("artist", "").strip()
                lines.append(f"  歌曲: {song if song else '未知'}")
                lines.append(f"  歌手: {artist if artist else '未知'}")
                album = ms.get("album", "")
                if album:
                    lines.append(f"  专辑: {album}")
                current_window = []
                for line in window:
                    if isinstance(line, dict):
                        current_window.append(f"    [{line.get('time', '?')}] {line.get('text', '')}")
                if current_window:
                    lines.append("  当前歌词:")
                    lines.extend(current_window)
                with open(music_ctx_path, "w", encoding="utf-8") as _f:
                    _f.write("\n".join(lines) + "\n\n")
            except Exception:
                pass
        else:
            if os.path.exists(music_ctx_path):
                try:
                    os.remove(music_ctx_path)
                except Exception:
                    pass

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
                self.mixed_state_var.set(d.get("mixed_state", False))
                self.claude_va_override_var.set(d.get("claude_va_override", False))
            except Exception:
                pass

    def _on_mixed_toggle(self):
        self._save_manual_va()

    def _save_manual_va(self):
        d = {
            "enabled": self.va_enabled_var.get(),
            "valence": round(self.va_valence_var.get(), 2),
            "arousal": round(self.va_arousal_var.get(), 2),
            "trust": round(self.va_trust_var.get(), 2),
            "mixed_state": self.mixed_state_var.get(),
            "claude_va_override": self.claude_va_override_var.get(),
        }
        try:
            from delegate_tools import atomic_write_json
            atomic_write_json(self.manual_va_path, d)
            self.va_status_label.config(text="已保存", foreground="green")
        except Exception as e:
            self.va_status_label.config(text=f"保存失败: {e}", foreground="red")

    def _clear_manual_va(self):
        self.va_enabled_var.set(False)
        self.va_valence_var.set(0.0)
        self.va_arousal_var.set(0.5)
        self.va_trust_var.set(0.8)
        self.mixed_state_var.set(False)
        self.claude_va_override_var.set(False)
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
            PROCESS_QUERY_INFORMATION = 0x0400
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_INFORMATION, False, pid)
            if not h:
                return False
            exit_code = ctypes.c_ulong()
            alive = False
            if kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
                alive = exit_code.value == STILL_ACTIVE
            kernel32.CloseHandle(h)
            return alive
        except Exception:
            return False

    def _boot_clean_pid(self, filename):
        from boot_guard import cleanup_stale_pid_file, read_pid_with_boot_token
        path = os.path.join(PROJECT_ROOT, filename)
        cleanup_stale_pid_file(path)
        pid, _ = read_pid_with_boot_token(path)
        return pid

    def _restart_daemon(self):
        daemon_pid = self._boot_clean_pid(".daemon.pid")
        if daemon_pid and self._pid_alive(daemon_pid):
            try:
                subprocess.run(["taskkill", "/f", "/pid", str(daemon_pid)],
                               capture_output=True,
                               creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
            except Exception:
                pass
        for pid_name in [".polling_loop.pid"]:
            old = self._boot_clean_pid(pid_name)
            if old and self._pid_alive(old):
                try:
                    subprocess.run(["taskkill", "/f", "/pid", str(old)],
                                   capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0)
                except Exception:
                    pass
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

    def _toggle_music_poll(self):
        music_alive = self._music_proc is not None and self._music_proc.poll() is None
        if not music_alive:
            music_alive = self._file_recently_updated(".music_state.json", 30)

        if music_alive:
            if self._music_proc and self._music_proc.poll() is None:
                try:
                    self._music_proc.terminate()
                    self._music_proc.wait(timeout=3)
                except Exception:
                    pass
            self._music_proc = None
            self._music_poll_btn.config(text="🎧 音乐轮询: 开")
        else:
            try:
                self._music_proc = subprocess.Popen(
                    [sys.executable, "music_poll.py"], cwd=PROJECT_ROOT,
                    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                )
                self._music_poll_btn.config(text="🎧 音乐轮询: 关")
            except Exception as e:
                self._music_poll_btn.config(text=f"🎧 失败: {e}")
                print(f"[console] 音乐拉起失败: {e}")

    def _build_daemon_status(self, parent_frame, ds: dict):
        if not ds or not ds.get("daemon_pid"):
            ttk.Label(parent_frame, text="守护进程状态: 无数据 (daemon 未运行?)",
                      foreground="orange").pack(fill=tk.X, padx=5, pady=2)
            return

        row2 = ttk.Frame(parent_frame)
        row2.pack(fill=tk.X, padx=5, pady=2)

        uptime = ds.get("uptime_seconds", 0)
        uptime_str = f"{uptime//3600}h{(uptime%3600)//60}m" if uptime else "?"

        music = ds.get("music_poll", {})
        music_state = music.get("state", "?")
        music_age = music.get("last_heartbeat_age_s")
        music_str = f"轮询: {music_state}"
        if music_age is not None:
            music_str += f" ({music_age}s前)"

        bark = ds.get("bark", {})
        bark_state = bark.get("state", "?")
        bark_str = f"Bark: {bark_state}"

        sched = ds.get("scheduled", {})
        sched_str = f"定时: {sched.get('window', '?')}"
        if sched.get("last_audit"):
            sched_str += f" 审计@{sched['last_audit'][11:19]}"

        colors = {"running": "green", "ok": "green", "exhausted": "red", "stopped": "gray", "error": "red"}

        ttk.Label(row2, text=f"守护: uptime {uptime_str}", foreground="green").pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text=music_str, foreground=colors.get(music_state, "gray")).pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text=bark_str, foreground=colors.get(bark_state, "gray")).pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=5)
        ttk.Label(row2, text=sched_str, foreground="green" if sched.get("daily_done") else "gray").pack(side=tk.LEFT, padx=5)

        last_up = ds.get("last_updated", "")
        if last_up:
            try:
                from datetime import datetime, timezone, timedelta
                up_dt = datetime.fromisoformat(last_up)
                age = (datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=8))) - up_dt).total_seconds()
                if age > 120:
                    ttk.Label(row2, text="|", foreground="#ccc").pack(side=tk.LEFT, padx=5)
                    ttk.Label(row2, text=f"⚠ daemon {int(age)}s无响应", foreground="red").pack(side=tk.LEFT, padx=5)
            except Exception:
                pass

        errors = ds.get("errors", [])
        if errors:
            last_err = errors[-1]
            ttk.Label(parent_frame,
                      text=f"最近错误 [{last_err.get('time','?')[11:19]}] {last_err.get('service','?')}: {last_err.get('error','?')[:80]}",
                      foreground="red", font=("", 7)).pack(fill=tk.X, padx=5, pady=1)
