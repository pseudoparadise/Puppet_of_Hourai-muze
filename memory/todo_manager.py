"""
todo_manager.py - 待办事项 GUI（艾森豪威尔四象限视图）
用法：python todo_manager.py
"""
import json
import os
import sys
import sqlite3
import tkinter as tk
from tkinter import ttk, messagebox

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from memory.memory_manager import get_todo_list, resolve_card, get_pending_todos

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

QUAD_COLORS = {
    "重要且紧急": ("#ffdddd", "#cc0000"),
    "重要不紧急": ("#ffffcc", "#cc8800"),
    "不重要但紧急": ("#ffeedd", "#cc6600"),
    "不重要不紧急": ("#eeeeee", "#888888"),
}


class TodoManager:
    def __init__(self, root_or_parent, standalone=True):
        self.standalone = standalone
        if standalone:
            self.root = root_or_parent
            self.root.title("待办事项 — 艾森豪威尔四象限")
            self.root.geometry("880x520")
            self.root.resizable(True, True)
            self.parent_frame = ttk.Frame(self.root)
            self.parent_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.root = root_or_parent.winfo_toplevel()
            self.parent_frame = root_or_parent

        # 工具栏
        toolbar = ttk.Frame(self.parent_frame)
        toolbar.pack(fill=tk.X, padx=5, pady=5)
        ttk.Button(toolbar, text="刷新", command=self.load_todos).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="📥 同步日记待办", command=self.sync_diary).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="标记完成", command=self.resolve_selected).pack(side=tk.LEFT, padx=3)
        ttk.Button(toolbar, text="查看卡片详情", command=self.show_card_detail).pack(side=tk.LEFT, padx=3)
        # 自动刷新
        self.auto_refresh = tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="30s自动刷新", variable=self.auto_refresh,
                        command=self._toggle_auto_refresh).pack(side=tk.LEFT, padx=10)

        # 象限筛选
        ttk.Label(toolbar, text="  象限:").pack(side=tk.LEFT, padx=(15, 3))
        self.quad_filter = ttk.Combobox(
            toolbar,
            values=["全部", "重要且紧急", "重要不紧急", "不重要但紧急", "不重要不紧急"],
            state="readonly", width=12
        )
        self.quad_filter.set("全部")
        self.quad_filter.pack(side=tk.LEFT, padx=3)
        self.quad_filter.bind("<<ComboboxSelected>>", lambda e: self.load_todos())

        self.status_label = ttk.Label(toolbar, text="")
        self.status_label.pack(side=tk.RIGHT, padx=10)

        # Treeview
        columns = ("quadrant", "title", "status", "source", "category", "deadline", "importance", "chord")
        self.tree = ttk.Treeview(self.parent_frame, columns=columns, show="headings", selectmode="browse")
        self.tree.heading("quadrant", text="象限", command=lambda: self.sort_by("quadrant"))
        self.tree.heading("title", text="标题", command=lambda: self.sort_by("title"))
        self.tree.heading("status", text="状态", command=lambda: self.sort_by("status"))
        self.tree.heading("source", text="来源", command=lambda: self.sort_by("source"))
        self.tree.heading("category", text="分类", command=lambda: self.sort_by("category"))
        self.tree.heading("deadline", text="到期", command=lambda: self.sort_by("deadline"))
        self.tree.heading("importance", text="重要度", command=lambda: self.sort_by("importance"))
        self.tree.heading("chord", text="和弦", command=lambda: self.sort_by("chord"))

        self.tree.column("quadrant", width=85)
        self.tree.column("title", width=220)
        self.tree.column("status", width=55)
        self.tree.column("source", width=45)
        self.tree.column("category", width=70)
        self.tree.column("deadline", width=85)
        self.tree.column("importance", width=50)
        self.tree.column("chord", width=120)

        self.tree.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # 右键菜单
        self.context_menu = tk.Menu(self.parent_frame, tearoff=0)
        self.context_menu.add_command(label="标记完成", command=self.resolve_selected)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="查看详情", command=self.show_card_detail)
        self.tree.bind("<Button-3>", self.show_context_menu)

        self.todos = []
        self.load_todos()

    def load_todos(self):
        # 合并 final + pending 待办
        final_todos = get_todo_list()
        for t in final_todos:
            t['status'] = ''
        pending_todos = get_pending_todos()
        self.todos = pending_todos + final_todos

        quad_filter = self.quad_filter.get()
        self.tree.delete(*self.tree.get_children())

        # 按到期日排序分组
        with_deadline = [t for t in self.todos if t['target_date']]
        without_deadline = [t for t in self.todos if not t['target_date']]
        with_deadline.sort(key=lambda x: x['target_date'])
        self.todos = with_deadline + without_deadline

        shown = 0
        pending_shown = 0
        for t in self.todos:
            if quad_filter != "全部" and t['quadrant'] != quad_filter:
                continue
            td = t['target_date'] if t['target_date'] else "—"
            ch = t['chord'] if t['chord'] else "—"
            source = "📅" if t.get('synced_from') == 'diary' else "💬"
            status = "⏳待审核" if t.get('status') == 'pending' else ""
            values = (t['quadrant'], t['title'], status, source, t['category'], td, t['importance'], ch)
            item = self.tree.insert("", tk.END, values=values)
            # 配色：pending 用浅黄色
            if t.get('status') == 'pending':
                self.tree.tag_configure('pending', background='#fffacd', foreground='#cc8800')
                self.tree.item(item, tags=('pending',))
                pending_shown += 1
            else:
                bg, fg = QUAD_COLORS.get(t['quadrant'], ("#ffffff", "#000000"))
                self.tree.tag_configure(t['quadrant'], background=bg, foreground=fg)
                self.tree.item(item, tags=(t['quadrant'],))
            shown += 1

        self.status_label.config(
            text=f"{shown} 项 (⏳{pending_shown}待审核)"
            if quad_filter == "全部"
            else f"{shown}/{len(self.todos)} 项"
        )

    def sync_diary(self):
        """同步日记事件中的待办到卡片库。"""
        try:
            from memory.memory_manager import sync_diary_todos_to_cards
            n = sync_diary_todos_to_cards(days_back=30)
            self.status_label.config(text=f"同步完成：新增 {n} 张待办卡片")
            self.load_todos()
        except Exception as e:
            self.status_label.config(text=f"同步失败: {e}")

    def _toggle_auto_refresh(self):
        """启用/停用 30 秒自动刷新。"""
        if self.auto_refresh.get():
            self._auto_refresh_loop()

    def _auto_refresh_loop(self):
        """自动刷新循环：每 30 秒 reload + sync。"""
        if not self.auto_refresh.get():
            return
        self.load_todos()
        self.root.after(30000, self._auto_refresh_loop)

    def resolve_selected(self):
        sel = self.tree.selection()
        if not sel:
            self.status_label.config(text="请先点选一项")
            return
        values = self.tree.item(sel[0])['values']
        title = values[1]
        # 从 todos 中找到匹配的卡片
        match = [t for t in self.todos if t['title'] == title]
        if not match:
            return
        card = match[0]
        if not messagebox.askyesno("确认", f"标记完成？\n\n{card['title']}\n{card['quadrant']}"):
            return
        if resolve_card(card['id']):
            self.status_label.config(text=f"已标记完成: {card['title'][:30]}")
            self.load_todos()
        else:
            self.status_label.config(text="标记失败，请检查卡片状态")

    def show_card_detail(self):
        sel = self.tree.selection()
        if not sel:
            return
        values = self.tree.item(sel[0])['values']
        title = values[1]
        match = [t for t in self.todos if t['title'] == title]
        if not match:
            return
        card = match[0]
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT content FROM cards WHERE id=?", (card['id'],))
        row = c.fetchone()
        conn.close()
        content = row[0] if row else "—"
        detail = (
            f"标题: {card['title']}\n"
            f"分类: {card['category']}  重要度: {card['importance']}\n"
            f"象限: {card['quadrant']}\n"
            f"到期: {card['target_date'] or '无'}\n"
            f"和弦: {card['chord'] or '无'}\n"
            f"效价: {card['valence']}  唤醒度: {card['arousal']}\n\n"
            f"内容:\n{content}"
        )
        top = tk.Toplevel(self.root)
        top.title("卡片详情")
        top.geometry("500x300")
        text = tk.Text(top, wrap=tk.WORD, font=("Microsoft YaHei", 10))
        text.insert("1.0", detail)
        text.config(state=tk.DISABLED)
        text.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def sort_by(self, col):
        col_map = {
            "quadrant": "quadrant",
            "title": "title",
            "status": "status",
            "source": "source",
            "category": "category",
            "deadline": "target_date",
            "importance": "importance",
            "chord": "chord",
        }
        key = col_map.get(col, "title")
        reverse = getattr(self, '_sort_reverse', False)
        if key == "target_date":
            self.todos.sort(key=lambda x: x.get(key, "9999") or "9999", reverse=reverse)
        else:
            self.todos.sort(key=lambda x: str(x.get(key, "")), reverse=reverse)
        self._sort_reverse = not reverse
        # 重新渲染
        self.tree.delete(*self.tree.get_children())
        for t in self.todos:
            quad = self.quad_filter.get()
            if quad != "全部" and t['quadrant'] != quad:
                continue
            td = t['target_date'] if t['target_date'] else "—"
            ch = t['chord'] if t['chord'] else "—"
            source = "📅" if t.get('synced_from') == 'diary' else "💬"
            status = "⏳待审核" if t.get('status') == 'pending' else ""
            values = (t['quadrant'], t['title'], status, source, t['category'], td, t['importance'], ch)
            item = self.tree.insert("", tk.END, values=values)
            if t.get('status') == 'pending':
                self.tree.tag_configure('pending', background='#fffacd', foreground='#cc8800')
                self.tree.item(item, tags=('pending',))
            else:
                self.tree.tag_configure(t['quadrant'], background=QUAD_COLORS[t['quadrant']][0])
                self.tree.item(item, tags=(t['quadrant'],))


if __name__ == "__main__":
    root = tk.Tk()
    app = TodoManager(root)
    root.mainloop()
