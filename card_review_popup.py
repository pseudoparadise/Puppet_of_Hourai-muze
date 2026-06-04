"""
card_review_popup.py — 写卡强制审核弹窗（全写卡入口统一闸门）
所有写卡路径必须调用 review_card_popup()，阻塞直到人做出选择。
支持独立运行（trigger.py/dreaming.py）和嵌入已有 GUI（console.py）。
"""
import os
import sys
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

CATEGORIES = [
    "deep_talks", "milestone", "turning_points", "commitments",
    "todo", "daily_life", "emotional", "interaction", "preferences",
    "erotic", "real_world", "habits"
]


def _parse_content_parts(content_str: str) -> tuple:
    if not content_str:
        return "", ""
    raw, summary = "", content_str
    if content_str.startswith("原话："):
        parts = content_str.split(" | 概括：", 1)
        raw = parts[0][3:]
        summary = parts[1] if len(parts) > 1 else ""
    elif content_str.startswith("概括："):
        summary = content_str[3:]
    return raw, summary


def review_card_popup(card_draft: dict, source_module: str, evidence: str = "") -> dict | None:
    """
    强制弹窗让人类审核卡片。阻塞调用，直到人做出选择。

    参数:
        card_draft: 完整的卡片草稿 dict
        source_module: 写卡来源
        evidence: 写卡依据

    返回:
        修改后的卡片 dict（人通过或编辑后）或 None（人拒绝）
    """
    result = {"action": "reject", "card": None}
    original_card = dict(card_draft)

    existing = tk._default_root
    if existing and existing.winfo_exists():
        root = tk.Toplevel(existing)
    else:
        root = tk.Tk()

    root.title(f"写卡审核 — {source_module}")
    root.attributes('-topmost', True)
    try:
        root.state('zoomed')
    except Exception:
        root.geometry("1020x720+100+50")
    root.resizable(True, True)
    root.configure(bg="#fafafa")
    root.lift()
    root.focus_force()

    header = tk.Frame(root, bg="#cc0000", height=42)
    header.pack(fill=tk.X)
    tk.Label(header, text=f"卡片审核 — {source_module}",
             font=("Microsoft YaHei", 13, "bold"), fg="white", bg="#cc0000").pack(side=tk.LEFT, padx=15, pady=8)
    tk.Label(header, text="不审核等会儿补卡累死你",
             font=("Microsoft YaHei", 9), fg="#ffcccc", bg="#cc0000").pack(side=tk.RIGHT, padx=15, pady=8)

    if evidence:
        ev_frame = tk.LabelFrame(root, text="写卡依据", font=("Microsoft YaHei", 9, "bold"),
                                 fg="#666", bg="#fafafa")
        ev_frame.pack(fill=tk.X, padx=10, pady=(8, 0))
        ev_text = tk.Text(ev_frame, height=3, wrap=tk.WORD, font=("Microsoft YaHei", 9),
                         bg="#fffbe6", relief=tk.SOLID, borderwidth=1)
        ev_text.pack(fill=tk.X, padx=5, pady=5)
        ev_text.insert("1.0", evidence[:600])
        ev_text.config(state=tk.DISABLED)

    main = tk.Frame(root, bg="#fafafa")
    main.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)

    left = tk.Frame(main, bg="#fafafa")
    left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 5))

    TYPES = ["fact", "event", "quote", "moment", "insight", "reflection"]
    fields = [
        ("ID", "id", 30),
        ("标题", "title", 30),
        ("分类", "category", 18),
        ("类型", "type", 12),
        ("重要度(1-10)", "importance", 4),
        ("关键词", "keywords", 30),
        ("和弦", "chord", 22),
        ("效价(-1~1)", "valence", 6),
        ("唤醒(-1~1)", "arousal", 6),
        ("目标日期", "target_date", 22),
    ]

    vars_map = {}
    for i, (label, key, width) in enumerate(fields):
        row = tk.Frame(left, bg="#fafafa")
        row.pack(fill=tk.X, pady=1)
        tk.Label(row, text=f"{label}:", width=11, anchor=tk.W,
                font=("Microsoft YaHei", 9), bg="#fafafa").pack(side=tk.LEFT)

        if key == "category":
            var = tk.StringVar(value=str(card_draft.get(key, "interaction") or "interaction"))
            cb = ttk.Combobox(row, textvariable=var, values=CATEGORIES, state="readonly", width=width)
            cb.pack(side=tk.LEFT, fill=tk.X, expand=True)
            vars_map[key] = var
        elif key == "type":
            var = tk.StringVar(value=str(card_draft.get(key, "fact") or "fact"))
            cb = ttk.Combobox(row, textvariable=var, values=TYPES, state="readonly", width=width)
            cb.pack(side=tk.LEFT, fill=tk.X, expand=True)
            vars_map[key] = var
        elif key in ("importance", "valence", "arousal"):
            var_cls = tk.IntVar if key == "importance" else tk.DoubleVar
            var = var_cls(value=card_draft.get(key, 5 if key == "importance" else (0.0 if key == "valence" else 0.5)))
            sb = ttk.Spinbox(row, textvariable=var,
                           from_=(-1.0 if key != "importance" else 1),
                           to=(1.0 if key != "importance" else 10),
                           increment=(0.05 if key != "importance" else 1),
                           width=width)
            sb.pack(side=tk.LEFT)
            vars_map[key] = var
        else:
            default = card_draft.get(key, "")
            if isinstance(default, (int, float)):
                default = str(default)
            var = tk.StringVar(value=str(default or ""))
            ttk.Entry(row, textvariable=var, width=width).pack(side=tk.LEFT, fill=tk.X, expand=True)
            vars_map[key] = var

    right = tk.Frame(main, bg="#fafafa")
    right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))

    raw_part, summary_part = _parse_content_parts(card_draft.get("content", "") or "")

    tk.Label(right, text="标志性原话:", anchor=tk.W,
            font=("Microsoft YaHei", 9, "bold"), bg="#fafafa").pack(fill=tk.X)
    raw_editor = tk.Text(right, height=4, wrap=tk.WORD, font=("Microsoft YaHei", 9))
    raw_editor.pack(fill=tk.X, pady=(0, 5))
    raw_editor.insert("1.0", raw_part)

    tk.Label(right, text="事件概括:", anchor=tk.W,
            font=("Microsoft YaHei", 9, "bold"), bg="#fafafa").pack(fill=tk.X)
    summary_editor = tk.Text(right, height=6, wrap=tk.WORD, font=("Microsoft YaHei", 9))
    summary_editor.pack(fill=tk.BOTH, expand=True)
    summary_editor.insert("1.0", summary_part)

    status_var = tk.StringVar(value="待审核 — 请通过/拒绝/编辑后通过")
    tk.Label(root, textvariable=status_var, font=("Microsoft YaHei", 9),
             fg="#888", bg="#fafafa").pack(fill=tk.X, padx=15, pady=(0, 3))

    btn_frame = tk.Frame(root, bg="#fafafa")
    btn_frame.pack(fill=tk.X, padx=15, pady=(3, 12))

    def _get_edited_card():
        raw = raw_editor.get("1.0", tk.END).strip()
        summary = summary_editor.get("1.0", tk.END).strip()
        content = f"原话：{raw} | 概括：{summary}" if raw else f"概括：{summary}"

        c = dict(card_draft)
        for key, var in vars_map.items():
            if key == "importance":
                c[key] = int(var.get())
            elif key in ("valence", "arousal"):
                c[key] = float(var.get())
            else:
                c[key] = var.get().strip()
        c["content"] = content
        c["title"] = c.get("title", "").strip()
        c["keywords"] = c.get("keywords", "").strip()
        c["chord"] = c.get("chord", "").strip()
        c["target_date"] = c.get("target_date", "").strip() or None
        c["id"] = c.get("id", "").strip()
        return c

    def _on_approve():
        card = _get_edited_card()
        if not card.get("title"):
            messagebox.showwarning("标题为空", "标题不能为空，请填写后再通过。", parent=root)
            return
        result["action"] = "approve"
        result["card"] = card
        root.destroy()

    def _on_reject():
        if not messagebox.askyesno("确认拒绝", "确定拒绝这张卡片吗？\n\n卡片将被丢弃，不会写入任何位置。", parent=root):
            return
        result["action"] = "reject"
        result["card"] = None
        root.destroy()

    def _on_close():
        result["action"] = "reject"
        result["card"] = None
        root.destroy()

    tk.Button(btn_frame, text="通过 (写入待审核池)", command=_on_approve,
             bg="#4caf50", fg="white", font=("Microsoft YaHei", 11, "bold"),
             width=22, height=2).pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text="拒绝 (丢弃)", command=_on_reject,
             bg="#f44336", fg="white", font=("Microsoft YaHei", 11),
             width=14, height=2).pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text="先编辑，再通过", command=lambda: [
        status_var.set("编辑模式 — 修改上方字段后点击「通过」"),
        raw_editor.focus_set()
    ], bg="#ff9800", fg="white", font=("Microsoft YaHei", 11),
             width=14, height=2).pack(side=tk.LEFT, padx=5)

    root.protocol("WM_DELETE_WINDOW", _on_close)

    if existing and existing.winfo_exists():
        root.grab_set()
        root.wait_window()
    else:
        root.mainloop()

    try:
        root.destroy()
    except Exception:
        pass

    if result["action"] == "reject":
        print(f"[card_review_popup] 卡片已被人类拒绝: {original_card.get('title', '?')}")
        return None

    approved = result["card"]
    edited = (
        approved.get("title") != original_card.get("title")
        or approved.get("content") != original_card.get("content")
        or approved.get("keywords") != original_card.get("keywords")
        or approved.get("category") != original_card.get("category")
        or approved.get("importance") != original_card.get("importance")
        or approved.get("chord") != original_card.get("chord")
        or approved.get("target_date") != original_card.get("target_date")
    )
    if edited or original_card.get("proposed_by") == "muze" or original_card.get("human_touched"):
        approved["human_touched"] = 1

    print(f"[card_review_popup] 卡片已审核通过: {approved.get('title', '?')} (human_touched={approved.get('human_touched', 0)})")
    return approved


if __name__ == "__main__":
    test_card = {
        "id": "20260604_test",
        "title": "测试卡片",
        "content": "原话：测试用户说了一段话 | 概括：测试事件概括",
        "keywords": "测试, 审核",
        "category": "interaction",
        "importance": 5,
        "valence": 0.5,
        "arousal": 0.6,
        "chord": "Cmaj7",
        "target_date": None,
        "proposed_by": "test",
        "proposed_at": datetime.now().isoformat(),
        "review_status": "pending",
    }
    result = review_card_popup(test_card, "test模块", "这是一条测试写卡依据：用户说'测试一下'")
    print(f"结果: {result['title'] if result else 'REJECTED'}")
