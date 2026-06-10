"""
console.py — phantom-trigger 统一控制台
用法: python console.py
"""
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
_sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), "memory"))
from crash_reporter import install as _install_crash
_install_crash()

import json
import sys, os
import time
import socket
import sqlite3
import subprocess
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime, timedelta, timezone

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

        # 混合态开关（月结晶触发条件）
        emo_row_mixed = ttk.Frame(emo_frame)
        emo_row_mixed.pack(fill=tk.X, pady=2)
        self.mixed_state_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(emo_row_mixed, text="混合态 [月结晶]（水+岩联动，人类判断置信100%）",
                        variable=self.mixed_state_var,
                        command=self._on_mixed_toggle).pack(side=tk.LEFT, padx=5)
        ttk.Label(emo_row_mixed, text="连续3张高imp锚定卡命中→触发月笼协奏迸发",
                  foreground="#8888cc", font=("", 8)).pack(side=tk.LEFT, padx=10)

        # Claude Code VA 远程开关
        emo_row_cc = ttk.Frame(emo_frame)
        emo_row_cc.pack(fill=tk.X, pady=2)
        self.claude_va_override_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(emo_row_cc, text="Claude Code → 用手动VA（开=100%手动不调DS | 关=自动估测）",
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

        # ── 每周自省通知 ──
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
                        text=f"待审阅: {pending['week']} ({len(pending['text'])}字)",
                        foreground="orange")
                    for w in self.refl_btn_frame.winfo_children():
                        w.destroy()
                    ttk.Button(self.refl_btn_frame, text="审阅",
                               command=lambda: _show_refl_editor(pending)).pack(side=tk.LEFT, padx=2)
                else:
                    self.refl_status_label.config(
                        text="无待审自省（周日自动生成）", foreground="gray")
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
                    "确定将这段自省注入 prompt_v1.txt 吗？\n\n"
                    "注入后，DS 的系统人格将包含这段自省，\n"
                    "影响 DS 在所有对话中的行为。"):
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
                if messagebox.askyesno("确认丢弃", "确定丢弃本周自省吗？\n下次生成需要等到下周日。"):
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
            # 关机后残留 stale 数据检测：started_at 超过 2 小时视为无效
            music_stale = False
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

        # 音乐开关：控制 preflight --music 是否注入歌曲上下文
        music_toggle_path = os.path.join(PROJECT_ROOT, ".music_toggle.json")
        music_enabled = False
        if os.path.exists(music_toggle_path):
            try:
                with open(music_toggle_path, "r", encoding="utf-8") as f:
                    music_enabled = json.load(f).get("enabled", False)
            except Exception:
                pass

        def _toggle_music():
            nonlocal music_enabled
            music_enabled = not music_enabled
            try:
                with open(music_toggle_path, "w", encoding="utf-8") as f:
                    json.dump({"enabled": music_enabled}, f)
            except Exception:
                pass
            self.refresh()

        btn_text = "🎵 家模式听歌: 开" if music_enabled else "🎵 家模式听歌: 关"
        btn_fg = "green" if music_enabled else "gray"
        self._music_btn = ttk.Button(row_m, text=btn_text, command=_toggle_music)
        self._music_btn.pack(side=tk.RIGHT, padx=5)

        # 第二行：歌词预览
        lyrics_frame = ttk.Frame(music_frame)
        lyrics_frame.pack(fill=tk.X, pady=(5, 0))
        lyrics_text = tk.Text(lyrics_frame, height=4, wrap=tk.WORD)
        lyrics_text.pack(fill=tk.X)
        window = []  # 预初始化，供后面音乐上下文写入使用
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

        # 为 preflight 准备音乐上下文（启用时写入，preflight 直接吃现成）
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
                # 当前歌词窗口（3句，已在上面算好）
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
            # 静音/未播放时删除上下文文件
            if os.path.exists(music_ctx_path):
                try:
                    os.remove(music_ctx_path)
                except Exception:
                    pass

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
        right_frame = ttk.LabelFrame(self, text="Persona / 维度", padding=5)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 维度雷达 + 滑块区
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

        # 基座人格
        persona_files = [
            ("动态人格", os.path.join(PROJECT_ROOT, "persona", "prompt_v1.txt")),
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
        import math
        canvas = self.radar_canvas
        canvas.delete("all")
        names = ["幽默", "温情", "包容", "创造力", "直率", "严肃", "任务导向"]
        n = len(names)
        cx, cy, r = 110, 110, 85
        # 背景网格
        for level in range(1, 6):
            lr = r * level / 5
            pts = []
            for i in range(n):
                angle = -math.pi / 2 + 2 * math.pi * i / n
                pts.extend([cx + lr * math.cos(angle), cy + lr * math.sin(angle)])
            canvas.create_polygon(pts, outline="#ddd", fill="", width=1)
        # 轴线
        for i in range(n):
            angle = -math.pi / 2 + 2 * math.pi * i / n
            canvas.create_line(cx, cy, cx + r * math.cos(angle), cy + r * math.sin(angle), fill="#ddd")
        # 数据多边形
        pts = []
        for i, name in enumerate(names):
            val = self.dim_vars.get(name, tk.IntVar(value=50)).get()
            angle = -math.pi / 2 + 2 * math.pi * i / n
            dr = r * val / 100
            pts.extend([cx + dr * math.cos(angle), cy + dr * math.sin(angle)])
        canvas.create_polygon(pts, outline="#536af5", fill="#d1d8ff", width=2)
        # 标签
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
        # 简单情感分析：统计积极/消极词
        positive = sum(content.count(w) for w in ["开心", "高兴", "好", "爱", "笑", "温暖", "漂亮", "舒服", "棒"])
        negative = sum(content.count(w) for w in ["难过", "累", "哭", "崩溃", "痛", "烦", "焦虑", "怕", "讨厌"])
        total = positive + negative or 1
        # 积极多 → 温情↑ 幽默↑; 消极多 → 包容↑ 严肃↓
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
        result = f"积极词{positive} 消极词{negative}"
        self._save_dimensions()
        messagebox.showinfo("校准完成", f"{fname}\n{result}\n维度已自动微调并保存。")

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
                        entry.get("time", ""),
                        heat,
                        silence,
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
        ttk.Entry(row0, textvariable=self.var_id, width=50).pack(side=tk.LEFT, fill=tk.X, expand=True)

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
        old_id = self._card["id"]
        new_id = self.var_id.get().strip()

        title = self.var_title.get().strip()
        raw_quote = self.raw_text.get("1.0", tk.END).strip()
        summary = self.summary_text.get("1.0", tk.END).strip()
        old_content = self._card.get("content") or ""
        old_keywords = self._card.get("keywords") or ""
        old_user_raw = self._card.get("user_raw") or ""
        content = f"原话：{raw_quote} | 概括：{summary}" if raw_quote else f"概括：{summary}"
        keywords = self.var_keywords.get().strip()
        category = self.var_category.get()
        importance = self.var_importance.get()
        valence = self.var_valence.get()
        arousal = self.var_arousal.get()
        chord = self.var_chord.get().strip()
        enabled = 1 if self.var_enabled.get() else 0
        resolved = 1 if self.var_resolved.get() else 0
        id_changed = new_id != old_id
        kw_changed = keywords != old_keywords
        quote_changed = raw_quote != old_user_raw
        summary_changed = summary != (old_content or "")  # 概括部分变了
        embed_changed = kw_changed or quote_changed or summary_changed

        if not new_id:
            self.status_var.set("ID不能为空")
            return
        if not title:
            self.status_var.set("标题不能为空")
            return

        if id_changed:
            if not messagebox.askyesno("确认修改ID",
                f"卡片ID将从\n「{old_id}」\n改为\n「{new_id}」\n\n"
                f"此操作会同步更新卡片库、关联边和 FAISS 索引。是否继续？"):
                return
        else:
            if not messagebox.askyesno("确认保存", f"确定保存对卡片「{title}」的修改吗？\n\n此操作直接写入数据库。"):
                return

        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()

            if id_changed:
                c.execute("SELECT COUNT(*) FROM cards WHERE id=?", (new_id,))
                if c.fetchone()[0] > 0:
                    messagebox.showerror("ID冲突", f"卡片ID「{new_id}」已存在，请换一个。")
                    conn.close()
                    return
                c.execute("UPDATE cards SET id=? WHERE id=?", (new_id, old_id))
                c.execute("UPDATE card_links SET card_id_a=? WHERE card_id_a=?", (new_id, old_id))
                c.execute("UPDATE card_links SET card_id_b=? WHERE card_id_b=?", (new_id, old_id))
                effective_id = new_id
            else:
                effective_id = old_id

            c.execute(
                "UPDATE cards SET title=?, content=?, keywords=?, user_raw=?, importance=?, category=?, "
                "valence=?, arousal=?, chord=?, enabled_in_context=?, resolved=?, human_touched=1 WHERE id=?",
                (title, content, keywords, raw_quote, importance, category, valence, arousal, chord, enabled, resolved, effective_id)
            )
            if c.rowcount == 0:
                messagebox.showerror("失败", "未找到对应卡片。")
                conn.close()
                return
            conn.commit()

            if resolved and not self._card.get("resolved"):
                try:
                    from memory.memory_manager import _log_resolution
                    _log_resolution(effective_id, title, "card_edit_save", "console编辑面板手动划掉")
                except Exception:
                    pass

            if id_changed:
                try:
                    self._reindex_rename(old_id, new_id)
                except Exception as e:
                    import traceback as _tb_ri
                    _tb_ri.print_exc()
                    messagebox.showerror("FAISS重索引异常", f"{type(e).__name__}: {e}")

            if id_changed or embed_changed:
                try:
                    self._re_embed(effective_id, title, content, keywords,
                                   kw_changed=kw_changed, quote_changed=quote_changed,
                                   summary_changed=summary_changed)
                except Exception as e:
                    import traceback as _tb_re
                    _tb_re.print_exc()
                    messagebox.showerror("卡片编辑-re-embed异常", f"{type(e).__name__}: {e}")

            self._card = {**self._card, "id": effective_id, "title": title, "content": content,
                          "keywords": keywords, "category": category, "importance": importance,
                          "valence": valence, "arousal": arousal, "chord": chord,
                          "enabled_in_context": enabled, "resolved": resolved}
            self.status_var.set(f"已保存: {effective_id}")
            messagebox.showinfo("成功", f"卡片「{title}」已保存。")
            self._search()
        except Exception as e:
            import traceback as _tb_s
            _tb_s.print_exc()
            messagebox.showerror("卡片编辑-保存异常", f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    def _re_embed(self, card_id, title, content, keywords,
                   kw_changed=False, quote_changed=False, summary_changed=False):
        """智能重嵌：只对变更的字段重新生成对应向量。ID 变更时全量重嵌。"""
        from memory.encoder import (
            embed, load_index, add_to_index, save_index, remove_from_index,
            build_embed_summary, build_embed_kw, build_embed_quote
        )

        id_changed = (card_id != self._card.get("id", ""))
        # ID 变更或全部未指定 → 全量重嵌
        full_regen = id_changed or not (kw_changed or quote_changed or summary_changed)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        card = dict(row) if row else {"title": title, "content": content, "keywords": keywords, "user_raw": "", "category": ""}
        conn.close()

        updates = []
        params = []

        if full_regen or summary_changed:
            vec_summary = embed(build_embed_summary(card))
            updates.append("embedding = ?")
            params.append(vec_summary.tobytes())
            # FAISS 索引更新
            remove_from_index(card_id)
            index = load_index()
            add_to_index(index, card_id, vec_summary)
            save_index(index)
            print(f"[editor] 摘要向量已更新: {card_id}")

        if full_regen or kw_changed:
            vec_kw = embed(build_embed_kw(card))
            updates.append("embedding_kw = ?")
            params.append(vec_kw.tobytes())
            print(f"[editor] 关键词向量已更新: {card_id}")

        if full_regen or quote_changed:
            vec_quote = embed(build_embed_quote(card))
            updates.append("embedding_quote = ?")
            params.append(vec_quote.tobytes())
            print(f"[editor] 原话向量已更新: {card_id}")

        if updates:
            conn = sqlite3.connect(DB_PATH)
            params.append(card_id)
            conn.execute(f"UPDATE cards SET {', '.join(updates)} WHERE id=?", params)
            conn.commit()
            conn.close()
            print(f"[editor] 重嵌完成: {card_id} ({len(updates)} 个向量)")
        else:
            print(f"[editor] 无变更，跳过重嵌: {card_id}")

    def _reindex_rename(self, old_id, new_id):
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT embedding FROM cards WHERE id=?", (new_id,))
        row = c.fetchone()
        conn.close()
        if not row or not row[0]:
            return
        from memory.encoder import load_index, save_index, remove_from_index, add_to_index
        import numpy as np
        vec = np.frombuffer(row[0], dtype=np.float32)
        remove_from_index(old_id)
        index = load_index()
        add_to_index(index, new_id, vec)
        save_index(index)
        print(f"[editor] FAISS 索引已迁移: {old_id} → {new_id}")


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

        # 左右分栏
        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 左侧：检索轮次列表
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
        ttk.Button(bottom, text="✓ 准确", command=lambda: self._mark("good")).pack(side=tk.LEFT, padx=4, ipadx=8, ipady=2)
        ttk.Button(bottom, text="✗ 不准", command=lambda: self._mark("bad")).pack(side=tk.LEFT, padx=4, ipadx=8, ipady=2)
        ttk.Button(bottom, text="清除标记", command=lambda: self._mark(None)).pack(side=tk.LEFT, padx=4, ipadx=8, ipady=2)
        self.fb_status = tk.Label(bottom, text="", fg="#666", font=("Microsoft YaHei", 10))
        self.fb_status.pack(side=tk.LEFT, padx=10)

        # QA 上下文面板
        qa_frame = ttk.LabelFrame(right, text="对话上下文 (chat_logs.json)", padding=5)
        qa_frame.pack(fill=tk.BOTH, expand=True, pady=5)
        self.qa_text = tk.Text(qa_frame, height=6, wrap=tk.WORD, font=("", 9))
        self.qa_text.pack(fill=tk.BOTH, expand=True)
        self.qa_text.config(state=tk.DISABLED)

        # 权重微调控件（Canvas + Scrollbar 滑动窗口）
        wt_frame = ttk.LabelFrame(right, text="检索权重微调 (写入 retrieval_weights.json)", padding=5)
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
            "w_semantic": ("语义", 0.0, 3.0, 0.02),
            "w_keyword": ("关键词", 0.0, 3.0, 0.02),
            "w_importance": ("重要度", 0.0, 3.0, 0.01),
            "w_anchor": ("锚定×.03", 0.0, 0.50, 0.002),
            "w_recency": ("时间×.03", 0.0, 1.0, 0.005),
            "w_diffusion": ("扩散×.03", 0.0, 0.50, 0.002),
            "w_decay": ("衰减×.03", 0.0, 0.50, 0.002),
            "w_presence_penalty": ("出现扣", 0.0, 0.30, 0.005),
            "w_repetition_penalty": ("重复扣", 0.0, 0.40, 0.005),
            "w_frequency_penalty": ("频次扣", 0.0, 0.10, 0.002),
            "frequency_penalty_cap": ("扣分上限", 0.0, 0.50, 0.005),
            "teleport_rate": ("传送", 0.0, 0.50, 0.005),
        }
        for key, (label, vmin, vmax, step) in all_labels.items():
            f = ttk.Frame(wt_inner)
            f.pack(side=tk.LEFT, padx=2)
            ttk.Label(f, text=label, font=("", 7)).pack()
            var = tk.DoubleVar()
            sb = ttk.Spinbox(f, textvariable=var, from_=vmin, to=vmax, increment=step, width=6)
            sb.pack()
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

    def _delete_selected(self):
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("未选中", "请在左侧列表选中要删除的 trace（可 Ctrl/Shift 多选）。")
            return
        if not messagebox.askyesno("确认删除", f"确定删除选中的 {len(selected)} 条检索记录吗？\n\n此操作不可撤销。"):
            return
        for tid in selected:
            self._traces.pop(tid, None)
        self._rewrite_traces()
        self._load()
        self.stats_label.config(text=f"已删除 {len(selected)} 条")

    def _delete_all(self):
        if not self._traces:
            return
        if not messagebox.askyesno("确认清空", f"确定删除全部 {len(self._traces)} 条检索记录吗？\n\n此操作不可撤销。"):
            return
        self._traces.clear()
        self._rewrite_traces()
        self._load()
        self.stats_label.config(text="已清空全部检索记录")

    def _rewrite_traces(self):
        import json as _json_trace
        lines = []
        if os.path.exists(self.TRACE_PATH):
            with open(self.TRACE_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        t = _json_trace.loads(line.strip())
                        if t.get("trace_id") in self._traces:
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
            import json, os
            path = os.path.join(os.path.dirname(__file__), "memory", "retrieval_weights.json")
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

    def _load(self):
        self._traces = {}
        for item in self.tree.get_children():
            self.tree.delete(item)
        for item in self.card_tree.get_children():
            self.card_tree.delete(item)

        if not os.path.exists(self.TRACE_PATH):
            self.stats_label.config(text="暂无检索记录")
            return

        good = bad = bad_lines = 0
        try:
            with open(self.TRACE_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        t = json.loads(line.strip())
                        if "cards" not in t:
                            continue  # 跳过 link 扩散日志，只保留检索结果
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
                            t.get("ts", "")[:16],
                            t.get("query", "")[:50],
                            t.get("va_tier", "?"),
                            len(cards),
                            tag_label,
                        ), iid=tid, tags=(tag,) if tag else ())
                    except json.JSONDecodeError as e:
                        bad_lines += 1
                        print(f"[feedback] JSON 解析失败 (行偏移={e.pos}): {line[:80]}", file=sys.stderr)
                    except KeyError as e:
                        bad_lines += 1
                        print(f"[feedback] 缺少字段 {e}: {line[:80]}", file=sys.stderr)
        except Exception as e:
            import traceback as _tb
            msg = f"[feedback] 读取 retrieval_traces.jsonl 失败:\n{_tb.format_exc()}"
            print(msg, file=sys.stderr)
            try:
                messagebox.showerror("召回反馈异常", f"读取 trace 文件失败:\n{e}\n\n详情已打印到终端")
            except Exception:
                pass
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

        best_idx = -1
        for i, e in enumerate(entries):
            content = str(e.get("content", ""))
            if query and len(query) >= 8 and query[:30] in content:
                best_idx = i
                break

        if best_idx < 0 and ts and entries:
            try:
                from datetime import datetime as _dt_qa
                trace_dt = _dt_qa.strptime(ts[:19], "%Y-%m-%d %H:%M:%S")
                best_idx = len(entries) - 1
                best_dist = 999999
                for i, e in enumerate(entries):
                    ets = e.get("timestamp", "")
                    if not ets:
                        continue
                    try:
                        entry_dt = _dt_qa.fromisoformat(ets.replace("+0000", "+00:00").replace("Z", "+00:00"))
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
            "type": "fact",
            "importance": importance,
            "proposed_by": "muze",
            "proposed_at": datetime.now(timezone.utc).isoformat(),
            "review_status": "pending",
            "human_touched": 1,
            "chord": chord,
            "valence": valence,
            "arousal": arousal,
            "target_date": target or None,
            "time_anchor": {"date": None, "fuzzy": None, "label": None, "days_until": None}
        }

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
        # ── 强制审核弹窗 ──
        try:
            from card_review_popup import review_card_popup
            evidence = f"人类手动写卡: {title}\n概括: {summary[:100]}"
            if _dup_info:
                evidence += f"\n⚠️ 重复检测: {_dup_info}"
            reviewed = review_card_popup(card, "console.py人类写卡", evidence)
            if reviewed is None:
                self.status_var.set("已拒绝")
                return
            card = reviewed
        except Exception as e:
            import traceback as _tb_rp
            _tb_rp.print_exc()
            raise RuntimeError(f"[console写卡审核弹窗失败] title={title}: {e}")

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
    from crash_reporter import install, crash_print
    install()

    root = tk.Tk()

    def _tk_error_handler(exc_type, exc_val, exc_tb):
        crash_print(exc_val, "console.py Tk回调异常")

    root.report_callback_exception = _tk_error_handler

    app = Console(root)
    root.mainloop()
