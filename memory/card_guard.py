"""
card_guard.py — 共享的写卡前置检查（拦截器 + 垃圾过滤 + embedding 语义去重 + 人工审核弹窗）
供 trigger.py 的 propose_card 路径和 auto-trigger 路径共同调用。
"""
import os, re, sqlite3, json, sys, tkinter as tk
from shared import zh_stop_chars, zh_extract_features
_STOP_CHARS = zh_stop_chars()  # compat
from tkinter import messagebox

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ── embedding 语义去重阈值：cosine ≥ 此值视为同一件事 ──
EMBED_DUP_THRESHOLD = 0.75

# 垃圾标题黑名单
_GARBAGE_TITLES = {
    "暂无计划", "没什么", "不知道", "没想好", "随便", "无", "暂无",
    "不想动", "懒得", "没力气", "好累", "累了",
    "无新承诺", "不要死在那个夏天",
}


def _extract_features(s: str) -> set:
    s = s.lower()
    chars = set(re.findall(r'[一-鿿]', s)) - _STOP_CHARS
    for t in re.findall(r'[a-z][a-z0-9]+', s):
        chars.add(t)
    return chars


def _cosine_similarity(vec_a, vec_b) -> float:
    import numpy as np
    dot = np.dot(vec_a, vec_b)
    norm = np.linalg.norm(vec_a) * np.linalg.norm(vec_b)
    if norm == 0:
        return 0.0
    return float(dot / norm)


def _is_semantic_dup(new_title: str, new_content: str,
                     old_title: str, old_content: str,
                     old_card_id: str = None,
                     old_vec = None,
                     new_vec = None) -> tuple[bool, float]:
    """
    用豆包 embedding 判断两张卡是否语义重复。返回 (is_dup, cosine_similarity)。
    旧卡向量优先级：old_vec（预存） > DB 缓存 > embed(old_text)。
    new_vec 由调用方预先计算，避免同一张新卡重复调用 embed API。
    """
    try:
        import numpy as _np
        from encoder import embed
        vec_new = new_vec
        if vec_new is None:
            new_text = new_title + " " + (new_content or "")
            vec_new = embed(new_text)

        # ── 旧卡向量：预存 > DB > 实时 embed ──
        vec_old = None
        if old_vec is not None:
            vec_old = _np.array(old_vec, dtype=_np.float32)
        elif old_card_id:
            try:
                import sqlite3
                _db = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
                _c = _db.cursor()
                _c.execute("SELECT embedding FROM cards WHERE id=?", (old_card_id,))
                _row = _c.fetchone()
                _db.close()
                if _row and _row[0]:
                    vec_old = _np.frombuffer(_row[0], dtype=_np.float32)
            except Exception:
                pass

        if vec_old is None:
            old_text = old_title + " " + (old_content or "")
            vec_old = embed(old_text)

        sim = _cosine_similarity(vec_new, vec_old)
        print(f"[语义去重] 「{new_title}」 vs 「{old_title}」 cosine={sim:.3f} "
              f"threshold={EMBED_DUP_THRESHOLD}")
        return sim >= EMBED_DUP_THRESHOLD, sim
    except Exception as e:
        print(f"[语义去重] embedding 调用失败: {e}，降级为硬拦截")
        return True, 1.0


def is_garbage_title(title: str) -> bool:
    title = title.strip()
    if len(title) <= 1:
        return True
    if title in _GARBAGE_TITLES:
        return True
    if title in {"好累", "不想动", "不想去", "不想做"}:
        return True
    return False


def show_conflict_popup(new_card: dict, old_card: dict, overlap: int, similarity: float) -> str:
    """
    卡牌语义冲突弹窗，展示新旧卡详情，让人做最终审核。
    返回: 'replace' | 'keep_both' | 'discard'
    """
    result = {'action': 'discard'}  # 默认丢弃，窗口关闭也安全

    root = tk.Tk()
    root.title("卡片冲突 — 人工审核")
    root.geometry("720x520")
    root.resizable(False, False)

    # ── 标题栏 ──
    header = tk.Label(root, text=f"语义冲突 — 请决定如何处理",
                      font=("Microsoft YaHei", 11, "bold"), fg="#cc0000")
    header.pack(pady=(10, 5))

    info = tk.Label(root,
                    text=f"新卡「{new_card.get('title','?')}」与已有卡片特征重叠({overlap})，"
                         f"embedding 余弦相似度={similarity:.3f}",
                    font=("Microsoft YaHei", 9), wraplength=680)
    info.pack(pady=(0, 10))

    # ── 左右分栏 ──
    frame = tk.Frame(root)
    frame.pack(fill=tk.BOTH, expand=True, padx=10)

    # 左栏 — 旧卡
    left = tk.LabelFrame(frame, text="已有卡片 (旧)", font=("Microsoft YaHei", 10, "bold"))
    left.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 5))

    old_text = tk.Text(left, wrap=tk.WORD, font=("Microsoft YaHei", 9),
                       width=42, height=16, state=tk.DISABLED)
    old_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    old_detail = (
        f"ID: {old_card.get('id', '?')}\n"
        f"标题: {old_card.get('title', '?')}\n"
        f"分类: {old_card.get('category', '?')}\n"
        f"重要度: {old_card.get('importance', '?')}\n"
        f"效价: {old_card.get('valence', 0.0):+.1f}  唤醒: {old_card.get('arousal', 0.5):.1f}\n"
        f"和弦: {old_card.get('chord', '无') or '无'}\n"
        f"到期: {old_card.get('target_date', '无') or '无'}\n"
        f"─── 内容 ───\n{old_card.get('content', '')[:600]}"
    )
    old_text.config(state=tk.NORMAL)
    old_text.insert("1.0", old_detail)
    old_text.config(state=tk.DISABLED)

    # 右栏 — 新卡
    right = tk.LabelFrame(frame, text="提议卡片 (新)", font=("Microsoft YaHei", 10, "bold"))
    right.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(5, 0))

    new_text = tk.Text(right, wrap=tk.WORD, font=("Microsoft YaHei", 9),
                       width=42, height=16, state=tk.DISABLED)
    new_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
    new_detail = (
        f"标题: {new_card.get('title', '?')}\n"
        f"分类: {new_card.get('category', '?')}\n"
        f"重要度: {new_card.get('importance', '?')}\n"
        f"效价: {new_card.get('valence', 0.0):+.1f}  唤醒: {new_card.get('arousal', 0.5):.1f}\n"
        f"和弦: {new_card.get('chord', '无') or '无'}\n"
        f"到期: {new_card.get('target_date', '无') or '无'}\n"
        f"─── 内容 ───\n{new_card.get('content', '')[:600]}"
    )
    new_text.config(state=tk.NORMAL)
    new_text.insert("1.0", new_detail)
    new_text.config(state=tk.DISABLED)

    # ── 按钮栏 ──
    btn_frame = tk.Frame(root, height=50)
    btn_frame.pack(fill=tk.X, pady=(10, 10), padx=20)

    def on_replace():
        result['action'] = 'replace'
        root.destroy()

    def on_keep_both():
        result['action'] = 'keep_both'
        root.destroy()

    def on_discard():
        result['action'] = 'discard'
        root.destroy()

    tk.Button(btn_frame, text="替换旧卡 (划掉旧卡，保留新卡)",
              command=on_replace, bg="#ffeecc", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text="保留两张 (都保留)",
              command=on_keep_both, bg="#ccffcc", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=5)
    tk.Button(btn_frame, text="丢弃新卡 (保留旧卡)",
              command=on_discard, bg="#ffcccc", font=("Microsoft YaHei", 9)).pack(side=tk.LEFT, padx=5)

    root.protocol("WM_DELETE_WINDOW", on_discard)  # 关窗口 = 丢弃新卡
    root.mainloop()
    return result['action']


def check_before_write(title: str, content: str, user_input: str,
                       new_card_context: dict = None) -> tuple[bool, str, dict | None]:
    """
    写卡前检查。返回 (should_block, reason, conflict_info)。
    conflict_info 非 None 表示需要人工弹窗审核，包含新旧卡详情。

    1. 垃圾标题 → 硬拦截
    2. 与现有 final 卡特征重叠 ≥2 → embedding 语义去重
       - 语义不同 → 放行
       - 语义重复 + 旧卡为 commitments/daily_life/todo → 返回 conflict_info，由调用方弹窗
       - 语义重复 + 旧卡为其他分类 → 硬拦截不划
    3. 与 pending 卡特征重叠 ≥2 → 同上
    """
    if is_garbage_title(title):
        return True, f"垃圾标题拦截: 「{title}」", None

    # ── 短期定时待办：不同时间点的同一动作不应语义去重 ──
    def _is_short_term_timed(new_ctx_dict):
        tdate = (new_ctx_dict or {}).get('target_date', '')
        if not tdate:
            return False
        try:
            from datetime import datetime as _dt_st, timedelta as _td_st
            target_dt = _dt_st.fromisoformat(tdate)
            if (_dt_st.now() + _td_st(hours=24)) >= target_dt:
                return True
        except Exception:
            pass
        return False
    _new_is_short_term = _is_short_term_timed(new_card_context)

    proposed_text = (title + " " + content).lower()
    proposed_features = zh_extract_features(proposed_text)
    new_ctx = new_card_context or {}

    # 预计算新卡 embedding，复用于所有语义对比
    new_vec = None
    try:
        from encoder import embed
        new_vec = embed((title + " " + (content or ""))[:512])
    except Exception:
        pass

    # 扫 cards.db
    db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute(
                "SELECT id, title, content, importance, category, valence, arousal, "
                "chord, target_date FROM cards "
                "WHERE review_status='final' AND resolved=0 ORDER BY created_at DESC LIMIT 20"
            )
            for row in c.fetchall():
                oid = row['id']
                otitle = row['title']
                ocontent = row['content']
                oimp = row['importance'] or 5
                ocat = row['category']

                if ocat in ('deep_talks', 'milestone', 'turning_points'):
                    continue
                if otitle.startswith('口癖：') or otitle.startswith('梗：'):
                    continue
                old_text = (otitle + " " + (ocontent or "")).lower()
                overlap = len(proposed_features & zh_extract_features(old_text))
                if overlap < 2:
                    continue
                if oimp >= 8:
                    continue
                # 短期定时待办：不同时间锚点的不做语义去重
                if _new_is_short_term:
                    old_tdate = row['target_date'] or ''
                    if old_tdate and old_tdate != (new_ctx or {}).get('target_date', ''):
                        continue

                is_dup, sim = _is_semantic_dup(title, content, otitle, ocontent,
                                                 old_card_id=oid, new_vec=new_vec)
                if not is_dup:
                    print(f"[语义去重] 「{title}」与「{otitle}」特征重叠({overlap})"
                          f"但语义不同，放行")
                    continue

                # 语义重复
                if ocat in ('commitments', 'daily_life', 'todo'):
                    old_card = {
                        "id": oid, "title": otitle, "content": ocontent or "",
                        "category": ocat, "importance": oimp,
                        "valence": row['valence'] or 0.0,
                        "arousal": row['arousal'] or 0.5,
                        "chord": row['chord'] or "",
                        "target_date": row['target_date'] or "",
                    }
                    new_card = {
                        "title": title, "content": content,
                        "category": new_ctx.get('category', 'interaction'),
                        "importance": new_ctx.get('importance', 5),
                        "valence": new_ctx.get('valence', 0.0),
                        "arousal": new_ctx.get('arousal', 0.5),
                        "chord": new_ctx.get('chord', ''),
                        "target_date": new_ctx.get('target_date', ''),
                    }
                    conn.close()
                    return True, f"语义重复 — 需人工审核: 「{title}」 vs 「{otitle}」", {
                        "new_card": new_card,
                        "old_card": old_card,
                        "overlap": overlap,
                        "similarity": sim,
                    }
                else:
                    print(f"[写卡拦截-embed] 新卡「{title}」与{ocat}卡「{otitle}」"
                          f"语义重复({overlap})，仅拦截不划")
                    conn.close()
                    return True, f"与{ocat}卡「{otitle}」语义重复({overlap})，仅拦截", None
            conn.close()
        except Exception as e:
            print(f"[写卡拦截] DB扫描跳过: {e}")

    # 扫 pending_cards.json
    pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
    if os.path.exists(pending_path):
        try:
            with open(pending_path, "r", encoding="utf-8") as f:
                pending = json.load(f)
            removed = False
            new_pending = []
            for pc in pending:
                ptitle = pc.get("title", "")
                ptext = (ptitle + " " + pc.get("content", "")).lower()
                overlap = len(proposed_features & zh_extract_features(ptext))
                if overlap < 2:
                    new_pending.append(pc)
                    continue
                if pc.get("importance", 5) >= 8:
                    new_pending.append(pc)
                    continue
                if ptitle.startswith('口癖：') or ptitle.startswith('梗：'):
                    new_pending.append(pc)
                    continue
                # 短期定时待办：不同时间锚点的不做语义去重
                if _new_is_short_term:
                    old_tdate = pc.get('target_date', '') or ''
                    if old_tdate and old_tdate != (new_ctx or {}).get('target_date', ''):
                        new_pending.append(pc)
                        continue

                is_dup, sim = _is_semantic_dup(title, content,
                                               pc.get("title", ""), pc.get("content", ""),
                                               old_card_id=pc.get("id", ""),
                                               old_vec=pc.get('_embed_vec'),
                                               new_vec=new_vec)
                if not is_dup:
                    print(f"[语义去重-pending] 「{title}」与「{pc.get('title','')}」"
                          f"特征重叠({overlap})但语义不同，放行")
                    new_pending.append(pc)
                    continue

                # pending 冲突：直接用弹窗结果
                old_card = {
                    "id": pc.get('id', ''), "title": pc.get('title', ''),
                    "content": pc.get('content', '') or "",
                    "category": pc.get('category', 'interaction'),
                    "importance": pc.get('importance', 5),
                    "valence": pc.get('valence', 0.0),
                    "arousal": pc.get('arousal', 0.5),
                    "chord": pc.get('chord', ''),
                    "target_date": pc.get('target_date', ''),
                }
                new_card = {
                    "title": title, "content": content,
                    "category": new_ctx.get('category', 'interaction'),
                    "importance": new_ctx.get('importance', 5),
                    "valence": new_ctx.get('valence', 0.0),
                    "arousal": new_ctx.get('arousal', 0.5),
                    "chord": new_ctx.get('chord', ''),
                    "target_date": new_ctx.get('target_date', ''),
                }
                old_pc = pc  # 保存引用供弹窗后处理
                # 先保留旧 pending，等弹窗结果决定
                new_pending.append(pc)
                print(f"[写卡拦截-pending] 与待审核卡「{pc.get('title','')}」"
                      f"语义重复({overlap})，弹窗审核")
                return True, f"语义重复 — 需人工审核: 「{title}」 vs 「{pc.get('title','')}」", {
                    "new_card": new_card,
                    "old_card": old_card,
                    "overlap": overlap,
                    "similarity": sim,
                    "old_is_pending": True,
                    "old_pc": old_pc,
                }
            if removed:
                from delegate_tools import atomic_write_json
                atomic_write_json(pending_path, new_pending)
        except Exception as e:
            print(f"[写卡拦截] pending扫描跳过: {e}")

    return False, "", None
