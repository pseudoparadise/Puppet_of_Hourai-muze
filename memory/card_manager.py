import json
import os
import sys
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

# 确保能找到 encoder 模块
sys.path.insert(0, os.path.dirname(__file__))
from encoder import embed, create_index, add_to_index, save_index

PENDING_PATH = os.path.join(os.path.dirname(__file__), "pending_cards.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

class CardManager:
    def __init__(self, root):
        self.root = root
        self.root.title("记忆卡片管理")
        self.root.geometry("700x500")
        self.root.resizable(False, False)

        # 界面布局
        notebook = ttk.Notebook(root)
        notebook.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # 标签页1：审核待定卡片
        self.pending_frame = ttk.Frame(notebook)
        notebook.add(self.pending_frame, text="等待审核")

        # 标签页2：查看正式卡片
        self.final_frame = ttk.Frame(notebook)
        notebook.add(self.final_frame, text="记忆卡片库")

        # 构建两个标签页的内容
        self.build_pending_tab()
        self.build_final_tab()

    # ---------- 等待审核 标签页 ----------
    def build_pending_tab(self):
        # 顶部按钮
        btn_frame = ttk.Frame(self.pending_frame)
        btn_frame.pack(fill=tk.X, pady=5)

        ttk.Button(btn_frame, text="刷新待审列表", command=self.load_pending).pack(side=tk.LEFT, padx=5)

        # 列表区域
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

        # 底部操作按钮
        action_frame = ttk.Frame(self.pending_frame)
        action_frame.pack(fill=tk.X, pady=5)

        ttk.Button(action_frame, text="通过 (存入记忆库)", command=self.approve_card).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="拒绝 (删除此卡片)", command=self.reject_card).pack(side=tk.LEFT, padx=5)

        self.status_label = ttk.Label(self.pending_frame, text="就绪")
        self.status_label.pack(side=tk.BOTTOM, fill=tk.X, pady=5)

        # 初始加载数据
        self.load_pending()

    def load_pending(self):
        """加载待审核卡片到列表"""
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
        """通过选中的卡片"""
        selected = self.pending_tree.selection()
        if not selected:
            self.status_label.config(text="请先在列表里点选一张卡片。")
            return

        card_id = selected[0]
        if not messagebox.askyesno("确认", f"确定通过卡片 {card_id} 并存入记忆库吗？"):
            return

        # 从 pending 文件读取完整信息
        pending = self._load_pending_list()
        card = next((c for c in pending if c["id"] == card_id), None)
        if not card:
            self.status_label.config(text="卡片信息读取失败。")
            return

        # 写入数据库并生成向量
        try:
            self._insert_into_db(card)
            # 从 pending 列表删除
            pending = [c for c in pending if c["id"] != card_id]
            self._save_pending_list(pending)
            # 刷新列表
            self.load_pending()
            self.status_label.config(text=f"卡片 {card_id} 已通过！")
        except Exception as e:
            self.status_label.config(text=f"入库失败: {e}")

    def reject_card(self):
        """拒绝选中的卡片"""
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
            vec = embed(card["content"])
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

            index = create_index()
            add_to_index(index, card["id"], vec)
            save_index(index)
        finally:
            conn.close()

    # ---------- 记忆卡片库 标签页 ----------
    def build_final_tab(self):
        # 按钮行
        btn_frame = ttk.Frame(self.final_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="刷新卡片库", command=self.load_final).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="删除选中卡片", command=self.delete_final_card).pack(side=tk.LEFT, padx=5)

        columns = ("id", "title", "category", "importance", "days_remaining", "enabled", "content")
        self.final_tree = ttk.Treeview(self.final_frame, columns=columns, show="headings", height=15)
        self.final_tree.heading("id", text="卡片ID")
        self.final_tree.heading("title", text="标题")
        self.final_tree.heading("category", text="分类")
        self.final_tree.heading("importance", text="重要度")
        self.final_tree.heading("days_remaining", text="剩余天数")
        self.final_tree.heading("enabled", text="活跃")
        self.final_tree.heading("content", text="内容")
        self.final_tree.column("id", width=140)
        self.final_tree.column("title", width=100)
        self.final_tree.column("category", width=80)
        self.final_tree.column("importance", width=60)
        self.final_tree.column("days_remaining", width=70)
        self.final_tree.column("enabled", width=50)
        self.final_tree.column("content", width=180)
        self.final_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        self.load_final()

    def load_final(self):
        from memory_manager import get_card_status
        for item in self.final_tree.get_children():
            self.final_tree.delete(item)

        cards = get_card_status()
        for card in cards:
            days = card["days_remaining"]
            if days == -1:
                days_str = "永久"
            elif days == 0:
                days_str = "已过期"
            else:
                days_str = f"{days}天"

            self.final_tree.insert("", tk.END, values=(
                card["id"],
                card["title"],
                card["category"],
                card["importance"],
                days_str,
                "是" if card["enabled"] else "否",
                ""  # content 列先留空，保持简洁
            ))

    def delete_final_card(self):
        """删除在记忆卡片库里选中的卡片"""
        selected = self.final_tree.selection()
        if not selected:
            messagebox.showwarning("未选中", "请先在卡片库里点选一张卡片。")
            return

        card_id = self.final_tree.item(selected[0], "values")[0]
        if not messagebox.askyesno("确认删除", f"确定要删除卡片 {card_id} 吗？\n这个操作无法撤销。\n如果卡片在FAISS索引中也会同步移除。"):
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

if __name__ == "__main__":
    root = tk.Tk()
    app = CardManager(root)
    root.mainloop()