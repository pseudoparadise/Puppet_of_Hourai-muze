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
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
# ── FIX: 导入 load_index / save_index 而不仅是 create_index ──
from encoder import embed, load_index, add_to_index, save_index, remove_from_index

PENDING_PATH = os.path.join(os.path.dirname(__file__), "pending_cards.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

class CardManager:
    def __init__(self, root_or_parent, standalone=True):
        self.standalone = standalone
        if standalone:
            self.root = root_or_parent
            self.root.title("记忆卡片管理")
            self.root.geometry("700x500")
            self.root.resizable(False, False)
            self.parent_frame = ttk.Frame(self.root)
            self.parent_frame.pack(fill=tk.BOTH, expand=True)
        else:
            self.root = root_or_parent.winfo_toplevel()
            self.parent_frame = root_or_parent

        notebook = ttk.Notebook(self.parent_frame)
        notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

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

        columns = ("title", "category", "importance", "valence", "arousal", "content")
        self.pending_tree = ttk.Treeview(self.pending_frame, columns=columns, show="headings", height=15)
        self.pending_tree.heading("title", text="标题")
        self.pending_tree.heading("category", text="分类")
        self.pending_tree.heading("importance", text="重要度")
        self.pending_tree.heading("valence", text="效价")
        self.pending_tree.heading("arousal", text="唤醒")
        self.pending_tree.heading("content", text="内容")
        self.pending_tree.column("title", width=120)
        self.pending_tree.column("category", width=80)
        self.pending_tree.column("importance", width=60)
        self.pending_tree.column("valence", width=55)
        self.pending_tree.column("arousal", width=55)
        self.pending_tree.column("content", width=400)
        self.pending_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.pending_tree.bind("<Double-1>", lambda e: self.show_card_detail())

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

        seen_iids = set()
        for card in pending:
            iid = card.get("id", f"unknown_{len(seen_iids)}")
            if iid in seen_iids:
                iid = f"{iid}_{len(seen_iids)}"
            seen_iids.add(iid)
            self.pending_tree.insert("", tk.END, values=(
                card.get("title", "无标题"),
                card.get("category", "?"),
                card.get("importance", "?"),
                f"{card.get('valence', 0.0):+.1f}",
                f"{card.get('arousal', 0.5):.1f}",
                card.get("content", "")[:120]
            ), iid=iid)

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

        # ── FIX: 先入库（含 embed），全部成功后才从 pending 移除（毒点3修复） ──
        try:
            self._insert_into_db(card)
        except Exception as e:
            self.status_label.config(text=f"入库失败: {e}")
            return  # 入库失败，pending 列表不修改

        # _insert_into_db 成功，安全移除 pending
        pending = [c for c in pending if c["id"] != card_id]
        self._save_pending_list(pending)
        self.load_pending()
        self.status_label.config(text=f"卡片 {card_id} 已通过！")

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
        from shared import load_json_safe
        return load_json_safe(PENDING_PATH, default=[], label="card_manager")

    def _save_pending_list(self, pending_list):
        # ── 毒点33修复：原子写入，避免 GUI 与后台并发截断 ──
        from delegate_tools import atomic_write_json
        atomic_write_json(PENDING_PATH, pending_list)

    def _insert_into_db(self, card):
        conn = sqlite3.connect(DB_PATH)
        try:
            # ── 阶段1.2：确保 chord 列存在，NULL → ''（修正2） ──
            try:
                conn.execute("ALTER TABLE cards ADD COLUMN chord TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            # ── target_date 列迁移 ──
            try:
                conn.execute("ALTER TABLE cards ADD COLUMN target_date TEXT")
            except sqlite3.OperationalError:
                pass

            # ── FIX: embed 调用加异常保护（毒点26修复：commit 移到最后） ──
            try:
                embed_content = card["content"]
                ch = card.get("chord") or ""
                if ch:
                    embed_content += f"\n[情绪纹理: {ch}]"
                vec = embed(embed_content)
            except Exception as e:
                raise RuntimeError(f"向量生成失败。请查看终端 [encoder] 日志了解详情。最后一次错误: {e}")

            vec_bytes = vec.tobytes()
            conn.execute("""
                INSERT OR REPLACE INTO cards (id, title, content, keywords, embedding, importance, category, review_status, chord, valence, arousal, target_date)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'final', ?, ?, ?, ?)
            """, (
                card["id"],
                card["title"],
                card["content"],
                card.get("keywords", ""),
                vec_bytes,
                card.get("importance", 5),
                card.get("category", "interaction"),
                card.get("chord") or "",
                card.get("valence", 0.0),
                card.get("arousal", 0.5),
                card.get("target_date")
            ))

            # ── FIX: 不再 create_index() 覆盖！改为 load→add→save ──
            index = load_index()
            add_to_index(index, card["id"], vec)
            save_index(index)

            # ── 毒点26修复：所有操作成功后最后 commit ──
            conn.commit()

            # 为新卡片建 link 边
            try:
                from .linker import build_links as _build_links
                _build_links(card["id"], vec)
            except Exception as _le:
                print(f"[link] 建边跳过: {_le}")
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def build_final_tab(self):
        # 分类过滤栏
        filter_frame = ttk.Frame(self.final_frame)
        filter_frame.pack(fill=tk.X, pady=5)
        ttk.Label(filter_frame, text="分类筛选:").pack(side=tk.LEFT, padx=5)
        self.final_cat_filter = ttk.Combobox(filter_frame, values=[
            "全部","milestone","commitments","turning_points","deep_talks",
            "interaction","preferences","real_world","daily_life","emotional","habits","erotic","todo"
        ], state="readonly", width=14)
        self.final_cat_filter.set("全部")
        self.final_cat_filter.pack(side=tk.LEFT, padx=5)
        self.final_cat_filter.bind("<<ComboboxSelected>>", lambda e: self.load_final())

        btn_frame = ttk.Frame(self.final_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="刷新", command=self.load_final).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="删除", command=self.delete_final_card).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="标记已解决", command=self.resolve_card).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="查看详情", command=self.show_card_detail).pack(side=tk.LEFT, padx=5)
        ttk.Button(btn_frame, text="回填向量", command=self.backfill_embeddings).pack(side=tk.LEFT, padx=5)

        columns = ("id", "title", "category", "importance", "links", "valence", "arousal", "days_remaining", "enabled", "resolved", "vec", "content")
        self.final_tree = ttk.Treeview(self.final_frame, columns=columns, show="headings", height=12)
        self.final_tree.heading("id", text="卡片ID")
        self.final_tree.heading("title", text="标题")
        self.final_tree.heading("category", text="分类")
        self.final_tree.heading("importance", text="重要度")
        self.final_tree.heading("links", text="link")
        self.final_tree.heading("valence", text="效价")
        self.final_tree.heading("arousal", text="唤醒")
        self.final_tree.heading("days_remaining", text="剩余")
        self.final_tree.heading("enabled", text="活跃")
        self.final_tree.heading("resolved", text="已解决")
        self.final_tree.heading("vec", text="向量")
        self.final_tree.heading("content", text="内容")
        self.final_tree.column("id", width=130)
        self.final_tree.column("title", width=110)
        self.final_tree.column("category", width=70)
        self.final_tree.column("importance", width=50)
        self.final_tree.column("links", width=40)
        self.final_tree.column("valence", width=55)
        self.final_tree.column("arousal", width=55)
        self.final_tree.column("days_remaining", width=50)
        self.final_tree.column("enabled", width=40)
        self.final_tree.column("resolved", width=50)
        self.final_tree.column("vec", width=40)
        self.final_tree.column("content", width=320)
        self.final_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.final_tree.bind("<Double-1>", lambda e: self.show_card_detail())
        self.load_final()

    def load_final(self):
        for item in self.final_tree.get_children():
            self.final_tree.delete(item)

        cat_filter = getattr(self, 'final_cat_filter', None)
        cat_filter_val = cat_filter.get() if cat_filter else "全部"

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            c = conn.cursor()
            c.execute("""
                SELECT id, title, category, importance,
                       valence, arousal,
                       created_at, last_referenced_at, enabled_in_context, resolved,
                       embedding IS NOT NULL as has_vec,
                       COALESCE(content,'') as content
                FROM cards WHERE review_status='final'
                ORDER BY created_at DESC
            """)
            rows = c.fetchall()
            from datetime import datetime, timezone as _tz
            now = datetime.now(_tz.utc).replace(tzinfo=None)

            # 批量加载 link 计数
            link_counts = {}
            try:
                _lc = conn.execute("""
                    SELECT card_id, COUNT(*) as cnt FROM (
                        SELECT card_id_a as card_id FROM card_links
                        UNION ALL SELECT card_id_b FROM card_links
                    ) GROUP BY card_id
                """).fetchall()
                link_counts = {r["card_id"]: r["cnt"] for r in _lc}
            except Exception:
                pass

            shown = 0
            for row in rows:
                if cat_filter_val != "全部" and row["category"] != cat_filter_val:
                    continue
                is_permanent = (row["category"] in ('milestone','commitments','deep_talks') or row["importance"] >= 8)
                if is_permanent:
                    days_str = "永久"
                else:
                    created = datetime.fromisoformat(row["created_at"]) if row["created_at"] else now
                    last = datetime.fromisoformat(row["last_referenced_at"]) if row["last_referenced_at"] else None
                    if created.tzinfo is not None:
                        created = created.astimezone(_tz.utc).replace(tzinfo=None)
                    if last is not None and last.tzinfo is not None:
                        last = last.astimezone(_tz.utc).replace(tzinfo=None)
                    ref = max(created, last) if last else created
                    elapsed = (now - ref).days
                    remaining = max(0, 30 - elapsed)
                    days_str = f"{remaining}天" if remaining > 0 else "已过期"

                _lcnt = link_counts.get(row["id"], 0)
                self.final_tree.insert("", tk.END, values=(
                    row["id"],
                    row["title"],
                    row["category"],
                    row["importance"],
                    str(_lcnt),
                    f"{row['valence']:+.1f}" if row['valence'] is not None else "+0.0",
                    f"{row['arousal']:.1f}" if row['arousal'] is not None else "0.5",
                    days_str,
                    "是" if row["enabled_in_context"] else "否",
                    "是" if row["resolved"] else "否",
                    "✓" if row["has_vec"] else "✗",
                    row["content"][:120]
                ), iid=row["id"])
                shown += 1
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

    def show_card_detail(self):
        """双击或按钮查看卡片完整内容"""
        tree = None
        for tab_tree in [self.final_tree, self.pending_tree, self.dormant_tree]:
            sel = tab_tree.selection()
            if sel:
                tree = tab_tree
                selected_iid = sel[0]
                break
        if not tree:
            messagebox.showinfo("提示", "请先点选一张卡片。")
            return

        values = tree.item(selected_iid, "values")
        cols = tree["columns"]
        detail_lines = []
        for i, col in enumerate(cols):
            if i < len(values):
                detail_lines.append(tree.heading(col)["text"] + ": " + str(values[i]))
        detail = "\n".join(detail_lines)
        
        card_id = values[0] if values else None
        if card_id:
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT content, keywords FROM cards WHERE id=?", (card_id,))
                row = c.fetchone()
                if row:
                    detail += "\n\n--- 完整内容 ---\n" + str(row[0])
                    detail += "\n\n关键词: " + str(row[1] or "")
                # 查询 link 邻居
                try:
                    lc = conn.execute(
                        "SELECT card_id_b, similarity FROM card_links WHERE card_id_a=? "
                        "UNION ALL SELECT card_id_a, similarity FROM card_links WHERE card_id_b=? "
                        "ORDER BY similarity DESC",
                        (card_id, card_id)
                    ).fetchall()
                    if lc:
                        detail += "\n\n--- 关联卡片 ---"
                        for lid, lsim in lc:
                            nc = conn.cursor()
                            nc.execute("SELECT title FROM cards WHERE id=?", (lid,))
                            nr = nc.fetchone()
                            label = nr[0][:30] if nr else lid
                            detail += f"\n  {label}  (cos={lsim:.3f})"
                    else:
                        detail += "\n\n(无关联卡片)"
                except Exception:
                    pass
            except Exception:
                pass
            finally:
                conn.close()
        
        messagebox.showinfo("卡片详情", detail)

    def resolve_card(self):
        selected = self.final_tree.selection()
        if not selected:
            messagebox.showwarning("未选中", "请先在卡片库里点选一张卡片。")
            return

        card_id = self.final_tree.item(selected[0], "values")[0]
        if not messagebox.askyesno("确认", f"确定将卡片 {card_id} 标记为已解决吗？"):
            return

        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE cards SET resolved = 1 WHERE id = ?", (card_id,))
            if c.rowcount == 0:
                messagebox.showerror("失败", f"未找到卡片 {card_id}。")
                conn.close()
                return
            conn.commit()
            conn.close()
            messagebox.showinfo("成功", f"卡片 {card_id} 已标记为已解决。")
            self.load_final()
        except Exception as e:
            messagebox.showerror("异常", f"操作异常: {e}")

    # ── FINAL-6: 已休眠卡片标签页 ──
    def build_dormant_tab(self):
        btn_frame = ttk.Frame(self.dormant_frame)
        btn_frame.pack(fill=tk.X, pady=5)
        ttk.Button(btn_frame, text="刷新休眠列表", command=self.load_dormant).pack(side=tk.LEFT, padx=5)

        columns = ("id", "title", "category", "importance", "valence", "arousal", "vec", "content")
        self.dormant_tree = ttk.Treeview(self.dormant_frame, columns=columns, show="headings", height=15)
        self.dormant_tree.heading("id", text="卡片ID")
        self.dormant_tree.heading("title", text="标题")
        self.dormant_tree.heading("category", text="分类")
        self.dormant_tree.heading("importance", text="重要度")
        self.dormant_tree.heading("valence", text="效价")
        self.dormant_tree.heading("arousal", text="唤醒")
        self.dormant_tree.heading("vec", text="向量")
        self.dormant_tree.heading("content", text="内容")
        self.dormant_tree.column("id", width=140)
        self.dormant_tree.column("title", width=100)
        self.dormant_tree.column("category", width=80)
        self.dormant_tree.column("importance", width=60)
        self.dormant_tree.column("valence", width=55)
        self.dormant_tree.column("arousal", width=55)
        self.dormant_tree.column("vec", width=40)
        self.dormant_tree.column("content", width=350)
        self.dormant_tree.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
        self.dormant_tree.bind("<Double-1>", lambda e: self.show_card_detail())

        action_frame = ttk.Frame(self.dormant_frame)
        action_frame.pack(fill=tk.X, pady=5)
        ttk.Button(action_frame, text="详情", command=self.show_dormant_detail).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="复权 (重新激活)", command=self.revive_card).pack(side=tk.LEFT, padx=5)
        ttk.Button(action_frame, text="彻底删除", command=self.delete_dormant_card).pack(side=tk.LEFT, padx=5)

        self.dormant_status = ttk.Label(self.dormant_frame, text="就绪")
        self.dormant_status.pack(side=tk.BOTTOM, fill=tk.X, pady=5)

        self.load_dormant()

    def show_dormant_detail(self):
        """查看休眠卡片完整内容，方便人工检查后决定是否删除。"""
        selected = self.dormant_tree.selection()
        if not selected:
            messagebox.showwarning("未选中", "请先在休眠列表里点选一张卡片。")
            return
        values = self.dormant_tree.item(selected[0], "values")
        card_id = values[0]
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT * FROM cards WHERE id=?", (card_id,))
            row = c.fetchone()
            conn.close()
            if row:
                cols = [d[0] for d in c.description]
                info = "\n".join(f"{k}: {v}" for k, v in zip(cols, row) if v is not None)
            else:
                info = f"卡片 {card_id} 不在数据库中。"
        except Exception as e:
            info = f"查询失败: {e}"
        messagebox.showinfo(f"休眠卡片详情 — {card_id}", info)

    def delete_dormant_card(self):
        """彻底删除休眠卡片（从 DB + FAISS 索引中移除）。"""
        selected = self.dormant_tree.selection()
        if not selected:
            messagebox.showwarning("未选中", "请先在休眠列表里点选一张卡片。")
            return
        card_id = self.dormant_tree.item(selected[0], "values")[0]
        title = self.dormant_tree.item(selected[0], "values")[1]
        if not messagebox.askyesno("确认彻底删除",
                                   f"确定要永久删除休眠卡片吗？\n\n"
                                   f"ID: {card_id}\n标题: {title}\n\n"
                                   f"这个操作无法撤销，卡片将从数据库和检索索引中彻底移除。"):
            return
        try:
            from memory_manager import delete_card
            success = delete_card(card_id)
            if success:
                messagebox.showinfo("成功", f"休眠卡片 {card_id} 已彻底删除。")
                self.load_dormant()
                self.load_final()
            else:
                messagebox.showerror("失败", f"删除卡片 {card_id} 失败。")
        except Exception as e:
            messagebox.showerror("异常", f"删除异常: {e}")

    def load_dormant(self):
        for item in self.dormant_tree.get_children():
            self.dormant_tree.delete(item)

        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("SELECT id, title, category, importance, valence, arousal, embedding IS NOT NULL as has_vec, content FROM cards WHERE review_status='final' AND enabled_in_context=0 ORDER BY id")
            rows = c.fetchall()
            for row in rows:
                vals = list(row)
                # 格式化 VA 值
                vals[4] = f"{vals[4]:+.1f}" if vals[4] is not None else "+0.0"
                vals[5] = f"{vals[5]:.1f}" if vals[5] is not None else "0.5"
                # 向量指示
                vals[6] = "✓" if vals[6] else "✗"
                if len(vals) > 7:
                    vals[7] = str(vals[7])[:150]
                self.dormant_tree.insert("", tk.END, values=vals, iid=row[0])
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


    def backfill_embeddings(self):
        """调 backfill_embeddings.py 批量回填老卡向量"""
        if not messagebox.askyesno("确认", "将扫描所有缺向量的卡片，调用豆包 embedding API 生成向量。\n\n"
                                            "已有向量的卡片自动跳过。是否继续？"):
            return
        try:
            from backfill_embeddings import main as do_backfill
            do_backfill()
            messagebox.showinfo("完成", "向量回填完成。请点「刷新」查看结果。")
            self.load_final()
            self.load_dormant()
        except Exception as e:
            messagebox.showerror("异常", f"回填失败: {e}")


if __name__ == "__main__":
    root = tk.Tk()
    app = CardManager(root)
    root.mainloop()