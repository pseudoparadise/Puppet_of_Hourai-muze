import os
import sqlite3
import sys
import tkinter as tk
from tkinter import ttk

from . import PROJECT_ROOT, DB_PATH


class CausalChainTab(ttk.Frame):
    COLORS = {"root": ("#e94560", "white"), "backward": ("#0f3460", "#c0c0c0"),
              "forward": ("#16213e", "#c0c0c0"), "parallel": ("#533483", "#c0c0c0")}
    _FONT = ("Microsoft YaHei", "SimHei", "TkDefaultFont")

    def __init__(self, parent):
        super().__init__(parent)
        self._link_source = None
        self._center_id = None
        self._node_items = {}
        self._edge_items = {}
        self._build()

    def _build(self):
        paned = ttk.PanedWindow(self, orient=tk.HORIZONTAL)
        paned.pack(fill=tk.BOTH, expand=True)

        left = ttk.Frame(paned, width=280)
        paned.add(left, weight=0)
        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        ttk.Label(left, text="搜索卡片:").pack(anchor=tk.W, padx=5, pady=(5, 0))
        search_frame = ttk.Frame(left)
        search_frame.pack(fill=tk.X, padx=5, pady=2)
        self._search_var = tk.StringVar()
        self._search_entry = ttk.Entry(search_frame, textvariable=self._search_var)
        self._search_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self._search_entry.bind("<Return>", lambda e: self._do_search())
        ttk.Button(search_frame, text="搜", width=3, command=self._do_search).pack(side=tk.RIGHT)

        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        self._card_list = tk.Listbox(list_frame, width=32)
        self._card_ids = []
        scroll = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self._card_list.yview)
        self._card_list.configure(yscrollcommand=scroll.set)
        self._card_list.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self._card_list.bind("<<ListboxSelect>>", self._on_list_select)
        self._card_list.bind("<Double-Button-1>", lambda e: self._on_list_double_click())

        ttk.Button(left, text="以选中卡为中心展开链", command=self._on_list_double_click).pack(fill=tk.X, padx=5, pady=2)

        bottom_bar = ttk.Frame(left)
        bottom_bar.pack(fill=tk.X, padx=5, pady=5)
        self._source_label = ttk.Label(bottom_bar, text="起点: (未选)", foreground="gray")
        self._source_label.pack(anchor=tk.W)
        ttk.Button(bottom_bar, text="清除起点", command=self._clear_source).pack(fill=tk.X, pady=2)

        self._status = ttk.Label(left, text="右键节点 → 设为起点/连到起点/断边", foreground="gray")
        self._status.pack(fill=tk.X, padx=5, pady=2)

        self._canvas_frame = ttk.Frame(right)
        self._canvas_frame.pack(fill=tk.BOTH, expand=True)
        self._canvas = tk.Canvas(self._canvas_frame, bg="#1a1a2e", highlightthickness=0)
        h_scroll = ttk.Scrollbar(self._canvas_frame, orient=tk.HORIZONTAL, command=self._canvas.xview)
        v_scroll = ttk.Scrollbar(self._canvas_frame, orient=tk.VERTICAL, command=self._canvas.yview)
        self._canvas.configure(xscrollcommand=h_scroll.set, yscrollcommand=v_scroll.set)
        self._canvas.grid(row=0, column=0, sticky="nsew")
        h_scroll.grid(row=1, column=0, sticky="ew")
        v_scroll.grid(row=0, column=1, sticky="ns")
        self._canvas_frame.grid_rowconfigure(0, weight=1)
        self._canvas_frame.grid_columnconfigure(0, weight=1)
        self._canvas.bind("<Button-1>", self._on_canvas_click)
        self._canvas.bind("<Button-3>" if sys.platform != "darwin" else "<Button-2>", self._on_canvas_right_click)

    def _do_search(self):
        q = self._search_var.get().strip()
        self._card_list.delete(0, tk.END)
        self._card_ids.clear()
        if not q:
            return
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, category, target_date FROM cards "
                "WHERE review_status='final' AND (title LIKE ? OR keywords LIKE ? OR content LIKE ?) LIMIT 30",
                (f"%{q}%", f"%{q}%", f"%{q}%")).fetchall()
            conn.close()
            for r in rows:
                date = (r["target_date"] or "")[:10]
                self._card_list.insert(tk.END, f"[{r['category'][:8]}] {r['title'][:30]}  {date}")
                self._card_ids.append(r["id"])
        except Exception as e:
            self._status.config(text=f"搜索失败: {e}")

    def _on_list_select(self, evt):
        cid = self._get_selected_id()
        if cid:
            self._status.config(text=f"已选: {cid[:40]}...")

    def _get_selected_id(self):
        sel = self._card_list.curselection()
        if not sel or sel[0] >= len(self._card_ids):
            return None
        return self._card_ids[sel[0]]

    def _on_list_double_click(self):
        cid = self._get_selected_id()
        if cid:
            self._center_id = cid
            self._draw_chain(cid)

    def _on_canvas_click(self, event):
        item = self._canvas.find_closest(event.x, event.y)
        tags = self._canvas.gettags(item[0]) if item else []
        for t in tags:
            if t.startswith("node_"):
                cid = t[5:]
                self._center_id = cid
                self._draw_chain(cid)
                return

    def _on_canvas_right_click(self, event):
        item = self._canvas.find_closest(event.x, event.y)
        tags = self._canvas.gettags(item[0]) if item else []
        cid = None
        for t in tags:
            if t.startswith("node_"):
                cid = t[5:]
                break
        if not cid:
            return
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="设为此卡为连线起点", command=lambda: self._set_source(cid))
        if self._link_source and self._link_source != cid:
            menu.add_command(label="连线到起点卡", command=lambda: self._link_to_source(cid))
        if self._link_source:
            menu.add_separator()
            menu.add_command(label="清除起点", command=self._clear_source)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _set_source(self, cid):
        self._link_source = cid
        try:
            conn = sqlite3.connect(DB_PATH)
            row = conn.execute("SELECT title FROM cards WHERE id=?", (cid,)).fetchone()
            conn.close()
            title = row[0][:25] if row else cid[:20]
        except Exception:
            title = cid[:20]
        self._source_label.config(text=f"起点: {title}", foreground="blue")

    def _clear_source(self):
        self._link_source = None
        self._source_label.config(text="起点: (未选)", foreground="gray")

    def _link_to_source(self, target_id):
        if not self._link_source or self._link_source == target_id:
            return
        try:
            from memory.linker import create_manual_link
            create_manual_link(self._link_source, target_id, relation="manual:causal")
            self._status.config(text=f"已连边: {self._link_source[:30]} → {target_id[:30]}")
            self._clear_source()
            if self._center_id:
                self._draw_chain(self._center_id)
        except Exception as e:
            self._status.config(text=f"连边失败: {e}")

    def _draw_chain(self, card_id):
        import math
        self._canvas.delete("all")
        self._node_items.clear()
        self._edge_items.clear()

        try:
            from memory.linker import get_causal_chain
            result = get_causal_chain(card_id, max_depth=4, manual_only=True)
        except Exception as e:
            self._canvas.create_text(400, 100, text=f"加载失败: {e}", fill="red", font=("", 14))
            return

        root = result.get("root")
        if not root:
            return
        chain = result.get("chain", [])
        edges = result.get("edges", [])

        nodes = {root["id"]: {"depth": 0, "direction": "root", "title": root["title"],
                               "date": root.get("target_date", "") or "", "manual": True, "confidence": 1.0}}
        for n in chain[1:]:
            nodes[n["id"]] = n

        children_of = {}
        for e in edges:
            fid, tid = e["from"], e["to"]
            children_of.setdefault(fid, []).append((tid, e))

        LEVEL_H, NODE_W, NODE_H = 110, 200, 48
        MIN_GAP = 60
        positions = {}

        def _layout(cur_id, depth, x_start, x_end):
            x = (x_start + x_end) // 2
            y = 40 + depth * LEVEL_H
            positions[cur_id] = (x, y)
            kids = children_of.get(cur_id, [])
            if not kids:
                return
            total = len(kids)
            avail = (x_end - x_start) - (total - 1) * MIN_GAP
            child_w = max(NODE_W, avail // total) if total > 0 else NODE_W
            total_w = total * (child_w + MIN_GAP) - MIN_GAP
            cx = x - total_w // 2 + child_w // 2
            for kid_id, _ in kids:
                _layout(kid_id, depth + 1, cx - child_w // 2, cx + child_w // 2)
                cx += child_w + MIN_GAP

        _layout(card_id, 0, 100, 900)

        all_x = [p[0] for p in positions.values()]
        all_y = [p[1] for p in positions.values()]
        cw = max(all_x) - min(all_x) + 400 if all_x else 800
        ch = max(all_y) + 150 if all_y else 600
        self._canvas.configure(scrollregion=(0, 0, cw, ch))

        for nid, (nx, ny) in positions.items():
            node = nodes.get(nid, {})
            direction = node.get("direction", "forward")
            bg, fg = self.COLORS.get(direction, self.COLORS["forward"])
            if nid == card_id:
                bg, fg = self.COLORS["root"]

            title = node.get("title", nid)[:22]
            date = (node.get("date") or node.get("target_date", ""))[:10]
            conf = node.get("confidence", 0)

            rect = self._canvas.create_rectangle(
                nx - NODE_W // 2, ny - NODE_H // 2,
                nx + NODE_W // 2, ny + NODE_H // 2,
                fill=bg, outline="#505070", width=1, tags=(f"node_{nid}", "node"))
            self._canvas.create_text(
                nx, ny - 8, text=title, fill=fg, font=(self._FONT[0], 10, "bold"),
                width=NODE_W - 10, tags=(f"node_{nid}", "node"))
            self._canvas.create_text(
                nx, ny + 14, text=f"{date}  c={conf:.2f}",
                fill="#808090", font=(self._FONT[0], 7), tags=(f"node_{nid}", "node"))
            self._node_items[nid] = rect

        for fid, kids in children_of.items():
            if fid not in positions:
                continue
            fx, fy = positions[fid]
            for tid, edge in kids:
                if tid not in positions:
                    continue
                tx, ty = positions[tid]
                style = "solid" if edge.get("manual") else "dashed"
                arrow = tk.LAST if edge.get("manual") else tk.NONE
                line_color = "#8080a0" if edge.get("manual") else "#505060"
                fy_bot = fy + NODE_H // 2
                ty_top = ty - NODE_H // 2
                self._canvas.create_line(
                    fx, fy_bot, tx, ty_top, fill=line_color, width=2,
                    arrow=arrow, dash=(4, 4) if style == "dashed" else (), tags=("edge",))
                mid_x, mid_y = (fx + tx) // 2, (fy_bot + ty_top) // 2
                self._canvas.create_text(
                    mid_x, mid_y, text=edge.get("label", ""),
                    fill="#9090b0", font=("Microsoft YaHei", 7), tags=("edge",))

        self._status.config(text=f"链: {len(positions)} 节点, {len(edges)} 条边 — 右键节点操作")

    def refresh_chain(self):
        if self._center_id:
            self._draw_chain(self._center_id)

    def jump_to(self, card_id):
        self._center_id = card_id
        self._draw_chain(card_id)
