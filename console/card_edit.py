import json
import os
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

from . import PROJECT_ROOT, DB_PATH, parse_content_parts


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
        top = ttk.Frame(self)
        top.pack(fill=tk.X, padx=10, pady=5)
        ttk.Label(top, text="卡片ID或标题关键词:").pack(side=tk.LEFT, padx=2)
        self.search_var = tk.StringVar()
        self.search_entry = ttk.Entry(top, textvariable=self.search_var, width=40)
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<Return>", lambda e: self._search())
        ttk.Button(top, text="搜索", command=self._search).pack(side=tk.LEFT, padx=2)
        ttk.Button(top, text="刷新", command=self._search).pack(side=tk.LEFT, padx=2)

        panes = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        panes.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

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

        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        self._build_editor(right)

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
        try:
            query = self.search_var.get().strip()
        except Exception as e:
            import traceback
            traceback.print_exc()
            messagebox.showerror("搜索异常", f"读取搜索框失败: {e}")
            return
        try:
            for item in self.result_tree.get_children():
                self.result_tree.delete(item)
        except Exception:
            pass

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        try:
            if query:
                c.execute(
                    "SELECT id, title, category, importance, content FROM cards WHERE review_status='final' "
                    "AND (id LIKE ? OR title LIKE ?) ORDER BY importance DESC LIMIT 50",
                    (f"%{query}%", f"%{query}%"))
            else:
                c.execute(
                    "SELECT id, title, category, importance, content FROM cards WHERE review_status='final' "
                    "ORDER BY importance DESC LIMIT 50")
            rows = c.fetchall()
        except Exception as e:
            import traceback
            traceback.print_exc()
            messagebox.showerror("搜索异常", f"数据库查询失败: {e}")
            conn.close()
            return
        for r in rows:
            preview = (r["content"] or "")[:60]
            self.result_tree.insert("", tk.END,
                values=(r["id"], r["title"], r["category"], r["importance"], preview), iid=r["id"])
        conn.close()
        self.status_var.set(f"找到 {len(self.result_tree.get_children())} 张卡片")

    def _on_select(self, event):
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
        raw_part, summary_part = parse_content_parts(content_str)
        self.raw_text.delete("1.0", tk.END)
        self.raw_text.insert("1.0", raw_part)
        self.summary_text.delete("1.0", tk.END)
        self.summary_text.insert("1.0", summary_part)
        self.status_var.set(f"已加载: {card_id}")

    def _save(self):
        if not self._card:
            self.status_var.set("请先搜索并选中一张卡片")
            return
        old_id = self._card["id"]
        new_id = self.var_id.get().strip()
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
        id_changed = new_id != old_id

        if not new_id:
            self.status_var.set("ID不能为空")
            return
        if not title:
            self.status_var.set("标题不能为空")
            return

        if id_changed:
            if not messagebox.askyesno("确认修改ID", f"卡片ID将从\n「{old_id}」\n改为\n「{new_id}」\n\n此操作会同步更新卡片库、关联边和 FAISS 索引。是否继续？"):
                return
        else:
            if not messagebox.askyesno("确认保存", f"确定保存对卡片「{title}」的修改吗？"):
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
                (title, content, keywords, raw_quote, importance, category, valence, arousal, chord, enabled, resolved, effective_id))
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
                self._reindex_rename(old_id, new_id)
            if id_changed or keywords != (self._card.get("keywords") or ""):
                self._re_embed(effective_id, title, content, keywords)

            self._card = {**self._card, "id": effective_id, "title": title, "content": content,
                          "keywords": keywords, "category": category, "importance": importance,
                          "valence": valence, "arousal": arousal, "chord": chord,
                          "enabled_in_context": enabled, "resolved": resolved}
            self.status_var.set(f"已保存: {effective_id}")
            messagebox.showinfo("成功", f"卡片「{title}」已保存。")
            self._search()
        except Exception as e:
            import traceback; traceback.print_exc()
            messagebox.showerror("卡片编辑-保存异常", f"{type(e).__name__}: {e}")
        finally:
            conn.close()

    def _re_embed(self, card_id, title, content, keywords):
        from memory.encoder import embed, load_index, add_to_index, save_index, remove_from_index, build_embed_summary
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        card = dict(row) if row else {"title": title, "content": content, "keywords": keywords, "user_raw": "", "category": ""}
        conn.close()
        vec = embed(build_embed_summary(card))
        try:
            remove_from_index(card_id)
        except Exception:
            pass
        index = load_index()
        add_to_index(index, card_id, vec)
        save_index(index)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE cards SET embedding=? WHERE id=?", (vec.tobytes(), card_id))
        conn.commit()
        conn.close()

    def _reindex_rename(self, old_id, new_id):
        import numpy as np
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT embedding FROM cards WHERE id=?", (new_id,))
        row = c.fetchone()
        conn.close()
        if not row or not row[0]:
            return
        from memory.encoder import load_index, save_index, remove_from_index, add_to_index
        vec = np.frombuffer(row[0], dtype=np.float32)
        remove_from_index(old_id)
        index = load_index()
        add_to_index(index, new_id, vec)
        save_index(index)
