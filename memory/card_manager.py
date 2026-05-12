"""
card_manager.py - 记忆卡片管理 GUI（修复版）

FIX #1: _insert_into_db 不再 create_index() 覆盖全部索引，改为 load→add→save
FIX #2: 集成 encoder 的 ID 映射系统
FIX #3: embed 调用添加异常处理，避免 GUI 闪退
"""
import json
import os
import sys
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(__file__))
# ── FIX: 导入 load_index / save_index 而不仅是 create_index ──
from encoder import embed, load_index, add_to_index, save_index, remove_from_index

PENDING_PATH = os.path.join(os.path.dirname(__file__), "pending_cards.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

class CardManager:
    def __init__(self, root):
        self.root = root
        self.root.title("记忆卡片管理")
        self.root.geometry("700x500")
        self.root.resizable(False, False)

        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        self.pending_frame = ttk.Frame(notebook)
        notebook.add(self.pending_frame, text="等待审核")

        self.final_frame = ttk.Frame(notebook)
        notebook.add(self.final_frame, text="记忆卡片库")

        # ── FINAL-6: 已休眠卡片标签页 ──
        self.dormant_frame = ttk.Frame(notebook)
        notebook.add(self.dormant_frame, text="已休眠卡片")

        self.build_pending_tab()
        self.build_final_tab()
        self.build_dormant_tab()

    def build_pending_tab(self):
        btn_frame = ttk.Frame(self.pending_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="刷新待审列表", command=self.load_pending).pack(side=tk.LEFT, padx=5)

        columns = ("title", "category", "importance", "content")
        self.pending_tree = ttk.Treeview(self.pending_frame, columns=columns, show="headings", height=15)
        self.pending_tree.heading("title", text="标题")
        self.pending_tree.heading("category", text="分类")
        self.pending_tree.heading("importance", text="重要度")
        self.pending_tree.heading("content", text="内容")
        self.pending_tree.column("title", width=120)
        self.pending_tree.column("category", width=80)
        self.pending_tree.column("importance", width=60)
        self.pending_tree.column("content", width=300)
        self.pending_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        action_frame = ttk.Frame(self.pending_frame)
        action_frame.pack(fill=tk.X, pady=5)
        ttk.Button(action_frame, text="通过 (存入记忆库)", command=self.approve_card).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="拒绝 (删除此卡片)", command=self.reject_card).pack(side=tk.LEFT, padx=5)

        self.status_label = ttk.Label(self.pending_frame, text="就绪")
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X, pady=5)

        self.load_pending()

    def load_pending(self):
        for item in self.pending_tree.get_children():
            self.pending_tree.delete(item)

        if not os.path.exists(PENDING_PATH):
            self.status_label.config(text="没有待审核的卡片。")
            return

        try:
            with open(PENDING_PATH, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except Exception as e:
            self.status_label.config(text=f"读取 pending 文件出错: {e}")
            return

        for card in pending:
            self.pending_tree.insert("", tk.END, values=(
                card.get("title", "无标题"),
                card.get("category", "?"),
                card.get("importance", "?"),
                card.get("content", "")[:50]
            ), iid=card.get("id"))

        self.status_label.config(text=f"共 {len(pending)} 张待审核卡片。")

    def approve_card(self):
        selected = self.pending_tree.selection()
        if not selected:
            self.status_label.config(text="请先在列表里点选一张卡片。")
            return

        card_id = selected[0]
        if not messagebox.askyesno("确认", f"确定通过卡片 {card_id} 并存入记忆库吗？"):
            return

        pending = self._load_pending_list()
        card = next((c for c in pending if c["id"] == card_id), None)
        if not card:
            self.status_label.config(text="卡片信息读取失败。")
            return

        try:
            self._insert_into_db(card)
            pending = [c for c in pending if c["id"] != card_id]
            self._save_pending_list(pending)
            self.load_pending()
            self.status_label.config(text=f"卡片 {card_id} 已通过！")
        except Exception as e:
            self.status_label.config(text=f"入库失败: {e}")

    def reject_card(self):
        selected = self.pending_tree.selection()
        if not selected:
            self.status_label.config(text="请先在列表里点选一张卡片。")
            return

        card_id = selected[0]
        if not messagebox.askyesno("确认", f"确定删除卡片 {card_id} 吗？\n这个操作无法撤销。"):
            return

        pending = self._load_pending_list()
        pending = [c for c in pending if c["id"] != card_id]
        self._save_pending_list(pending)
        self.load_pending()
        self.status_label.config(text=f"卡片 {card_id} 已删除。")

    def _load_pending_list(self):
        if not os.path.exists(PENDING_PATH):
            return []
        with open(PENDING_PATH, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save_pending_list(self, pending_list):
        with open(PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(pending_list, f, ensure_ascii=False, indent=2)

    def _insert_into_db(self, card):
        conn = sqlite3.connect(DB_PATH)
        try:
            # ── FIX: embed 调用加异常保护 ──
            try:
                vec = embed(card["content"])
            except Exception as e:
                raise RuntimeError(f"向量生成失败（可能是豆包API连接问题，已自动重试3次仍失败）: {e}")

            vec_bytes = vec.tobytes()
            conn.execute("""
                INSERT OR REPLACE INTO cards (id, title, content, keywords, embedding, importance, category, review_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'final')
            """, (
                card["id"],
                card["title"],
                card["content"],
                card.get("keywords", ""),
                vec_bytes,
                card.get("importance", 5),
                card.get("category", "interaction")
            ))
            conn.commit()

            # ── FIX: 不再 create_index() 覆盖！改为 load→add→save ──
            index = load_index()
            add_to_index(index, card["id"], vec)
            save_index(index)
        finally:
            conn.close()

    def build_final_tab(self):
        btn_frame = ttk.Frame(self.final_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="刷新卡片库", command=self.load_final).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="删除选中卡片", command=self.delete_final_card).pack(side=tk.LEFT, padx=5)

        columns = ("id", "title", "category", "importance", "days_remaining", "enabled", "resolved", "content")
        self.final_tree = ttk.Treeview(self.final_frame, columns=columns, show="headings", height=15)
        self.final_tree.heading("id", text="卡片ID")
        self.final_tree.heading("title", text="标题")
        self.final_tree.heading("category", text="分类")
        self.final_tree.heading("importance", text="重要度")
        self.final_tree.heading("days_remaining", text="剩余天数")
        self.final_tree.heading("enabled", text="活跃")
        self.final_tree.heading("resolved", text="已解决")
        self.final_tree.heading("content", text="内容")
        self.final_tree.column("id", width=140)
        self.final_tree.column("title", width=100)
        self.final_tree.column("category", width=80)
        self.final_tree.column("importance", width=60)
        self.final_tree.column("days_remaining", width=70)
        self.final_tree.column("enabled", width=50)
        self.final_tree.column("resolved", width=50)
        self.final_tree.column("content", width=180)
        self.final_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.load_final()

    def load_final(self):
        for item in self.final_tree.get_children():
            self.final_tree.delete(item)

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("""
                SELECT id, title, category, importance,
                       created_at, last_referenced_at, enabled_in_context, resolved
                FROM cards WHERE review_status='final'
                ORDER BY created_at DESC
            """)
            rows = c.fetchall()
            from datetime import datetime, timezone as _tz
            now = datetime.now(_tz.utc).replace(tzinfo=None)  # naive UTC
            for row in rows:
                is_permanent = (row["category"] in ('milestone','commitments','deep_talks') or row["importance"] >= 8)
                if is_permanent:
                    days_str = "永久"
                else:
                    created = datetime.fromisoformat(row["created_at"]) if row["created_at"] else now
                    last = datetime.fromisoformat(row["last_referenced_at"]) if row["last_referenced_at"] else None
                    # 统一转为 UTC naive 再比较
                    if created.tzinfo is not None:
                        created = created.astimezone(_tz.utc).replace(tzinfo=None)
                    if last is not None and last.tzinfo is not None:
                        last = last.astimezone(_tz.utc).replace(tzinfo=None)
                    ref = max(created, last) if last else created
                    elapsed = (now - ref).days
                    remaining = max(0, 30 - elapsed)
                    days_str = f"{remaining}天" if remaining > 0 else "已过期"

                self.final_tree.insert("", tk.END, values=(
                    row["id"],
                    row["title"],
                    row["category"],
                    row["importance"],
                    days_str,
                    "是" if row["enabled_in_context"] else "否",
                    "是" if row["resolved"] else "否",
                    ""
                ))
        finally:
            conn.close()

    def delete_final_card(self):
        selected = self.final_tree.selection()
        if not selected:
            messagebox.showwarning("未选中", "请先在卡片库里点选一张卡片。")
            return

        card_id = self.final_tree.item(selected[0], "values")[0]
        if not messagebox.askyesno("确认删除", f"确定要删除卡片 {card_id} 吗？\n这个操作无法撤销。"):
            return

        try:
            from memory_manager import delete_card
            success = delete_card(card_id)
            if success:
                messagebox.showinfo("成功", f"卡片 {card_id} 已删除。")
                self.load_final()
            else:
                messagebox.showerror("失败", f"删除卡片 {card_id} 失败。")
        except Exception as e:
            messagebox.showerror("异常", f"删除异常: {e}")

    # ── FINAL-6: 已休眠卡片标签页 ──
    def build_dormant_tab(self):
        btn_frame = ttk.Frame(self.dormant_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="刷新休眠列表", command=self.load_dormant).pack(side=tk.LEFT, padx=5)

        columns = ("id", "title", "category", "importance", "content")
        self.dormant_tree = ttk.Treeview(self.dormant_frame, columns=columns, show="headings", height=15)
        self.dormant_tree.heading("id", text="卡片ID")
        self.dormant_tree.heading("title", text="标题")
        self.dormant_tree.heading("category", text="分类")
        self.dormant_tree.heading("importance", text="重要度")
        self.dormant_tree.heading("content", text="内容")
        self.dormant_tree.column("id", width=140)
        self.dormant_tree.column("title", width=100)
        self.dormant_tree.column("category", width=80)
        self.dormant_tree.column("importance", width=60)
        self.dormant_tree.column("content", width=180)
        self.dormant_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        action_frame = ttk.Frame(self.dormant_frame)
        action_frame.pack(fill=tk.X, pady=5)
        ttk.Button(action_frame, text="复权 (重新激活)", command=self.revive_card).pack(side=tk.LEFT, padx=5)

        self.dormant_status = ttk.Label(self.dormant_frame, text="就绪")
        self.dormant_status.pack(side=tk.BOTTOM, fill=tk.X, pady=5)

        self.load_dormant()

    def load_dormant(self):
        for item in self.dormant_tree.get_children():
            self.dormant_tree.delete(item)

        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("SELECT id, title, category, importance, content FROM cards WHERE review_status='final' AND enabled_in_context=0 ORDER BY id")
            rows = c.fetchall()
            for row in rows:
                self.dormant_tree.insert("", tk.END, values=row, iid=row[0])
            self.dormant_status.config(text=f"共 {len(rows)} 张休眠卡片。")
        except Exception as e:
            self.dormant_status.config(text=f"查询失败: {e}")
        finally:
            conn.close()

    def revive_card(self):
        selected = self.dormant_tree.selection()
        if not selected:
            self.dormant_status.config(text="请先在休眠列表里点选一张卡片。")
            return

        card_id = selected[0]
        if not messagebox.askyesno("确认", f"确定复权卡片 {card_id} 吗？\n它将重新加入活跃记忆库。"):
            return

        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("UPDATE cards SET enabled_in_context = 1 WHERE id = ?", (card_id,))
            conn.commit()
            if c.rowcount > 0:
                messagebox.showinfo("成功", f"卡片 {card_id} 已复权。")
                self.load_dormant()
                self.load_final()
            else:
                messagebox.showerror("失败", f"卡片 {card_id} 复权失败。")
        except Exception as e:
            messagebox.showerror("异常", f"复权异常: {e}")
        finally:
            conn.close()


if __name__ == "__main__":
    root = tk.Tk()
    app = CardManager(root)
    root.mainloop()