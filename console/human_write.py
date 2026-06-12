import json
import os
import sqlite3
import tkinter as tk
from tkinter import ttk
from datetime import datetime, timezone

from . import PROJECT_ROOT, DB_PATH


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
        ttk.Combobox(row1b, textvariable=self.var_category, values=self.CATEGORIES, state="readonly", width=18).pack(side=tk.LEFT, padx=2)
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
        ttk.Label(row_target, text="(可选, YYYY-MM-DD 或 YYYY-MM-DD HH:MM)", foreground="gray", font=("", 8)).pack(side=tk.LEFT, padx=5)

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
            "id": card_id, "title": title, "content": content, "keywords": keywords,
            "user_raw": raw_quote, "category": category, "type": "fact",
            "importance": importance, "proposed_by": "muze",
            "proposed_at": datetime.now(timezone.utc).isoformat(),
            "review_status": "pending", "human_touched": 1,
            "chord": chord, "valence": valence, "arousal": arousal,
            "target_date": target or None,
            "time_anchor": {"date": None, "fuzzy": None, "label": None, "days_until": None}
        }

        pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
        pending = []
        if os.path.exists(pending_path):
            try:
                with open(pending_path, "r", encoding="utf-8") as f:
                    pending = json.load(f)
            except Exception:
                pass

        _dup_info = None
        for pc in pending:
            if pc.get("title", "") == title or content[:30] in pc.get("content", ""):
                _dup_info = f"待审核池: {pc.get('title', '')}"
                break
        if not _dup_info:
            try:
                db_conn = sqlite3.connect(DB_PATH)
                db_c = db_conn.cursor()
                db_c.execute("SELECT id, title FROM cards WHERE title=? LIMIT 1", (title,))
                db_row = db_c.fetchone()
                if db_row:
                    _dup_info = f"卡片库: {db_row[1]}"
                db_conn.close()
            except Exception:
                pass

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
            import traceback; traceback.print_exc()
            raise RuntimeError(f"[console写卡审核弹窗失败] title={title}: {e}")

        pending.append(card)
        try:
            from delegate_tools import atomic_write_json
            atomic_write_json(pending_path, pending)
            self.status_var.set(f"已写入: {card_id}")
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
