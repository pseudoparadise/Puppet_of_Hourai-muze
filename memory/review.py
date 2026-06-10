"""
review.py - 记忆卡片审核闸门（修复版）

FIX: approve_card 不再 create_index() 覆盖全部索引，改为 load→add→save
FIX: load_pending 加固 — JSON 损坏时备份并重建，避免崩溃
"""
import json
import os
import sys
import sqlite3
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
# ── FIX: 导入 load_index ──
from encoder import (
    embed, load_index, add_to_index, save_index,
    build_embed_summary, build_embed_kw, build_embed_quote
)

PENDING_PATH = os.path.join(os.path.dirname(__file__), "pending_cards.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

def load_pending():
    from shared import load_json_safe
    return load_json_safe(PENDING_PATH, default=[], label="review")

def save_pending(pending_list):
    from delegate_tools import atomic_write_json
    atomic_write_json(PENDING_PATH, pending_list)

def approve_card(card):
    """通过审核：写入数据库，生成三个向量，摘要向量加入FAISS索引"""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR REPLACE INTO cards (id, title, content, keywords, embedding, embedding_kw, embedding_quote,
            importance, category, type, review_status, enabled_in_context, chord, valence, arousal, target_date, user_raw, human_touched)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'final', 1, ?, ?, ?, ?, ?, ?)
        """, (
            card["id"], card["title"], card["content"], card["keywords"],
            None, None, None,  # 三个向量先占位，生成后 UPDATE
            card.get("importance", 5), card.get("category", "interaction"),
            card.get("type", "fact"),
            card.get("chord") or "", card.get("valence", 0.0), card.get("arousal", 0.5),
            card.get("target_date"), card.get("user_raw", ""),
            card.get("human_touched", 0)
        ))
        conn.commit()

        # 复用 pending 预计算向量（仅 summary），kw/quote 始终生成
        pre_vec = card.get("_embed_vec")
        if pre_vec is not None:
            vec_summary = np.array(pre_vec, dtype=np.float32)
            print(f"  📎 复用预计算向量 ({vec_summary.shape[0]} 维)")
        else:
            vec_summary = embed(build_embed_summary(card))

        vec_kw = embed(build_embed_kw(card))
        vec_quote = embed(build_embed_quote(card))

        # 写三个向量到 DB
        conn.execute(
            "UPDATE cards SET embedding=?, embedding_kw=?, embedding_quote=? WHERE id=?",
            (vec_summary.tobytes(), vec_kw.tobytes(), vec_quote.tobytes(), card["id"])
        )
        conn.commit()

        # FAISS 只入摘要向量
        index = load_index()
        add_to_index(index, card["id"], vec_summary)
        save_index(index)
        print(f"  ✅ 已通过并入库（三向量）: {card['id']}")
    except Exception as e:
        print(f"  ❌ 入库失败: {e}")
    finally:
        conn.close()

def reject_card(card):
    print(f"  🗑️ 已拒绝: {card['id']}")

def main():
    pending = load_pending()
    if not pending:
        print("没有待审核的卡片。")
        return

    print(f"共有 {len(pending)} 张待审核卡片：\n")
    for i, card in enumerate(pending, 1):
        print(f"[{i}] {card['id']}")
        print(f"    标题: {card['title']}")
        print(f"    内容: {card['content']}")
        print(f"    分类: {card.get('category','?')}  重要度: {card.get('importance','?')}")
        print()

        while True:
            choice = input("通过？(y/n, 默认n): ").strip().lower()
            if choice in ("y", "n", ""):
                break
            print("请输入 y 或 n")

        if choice == "y":
            approve_card(card)
        else:
            reject_card(card)
        print()

    save_pending([])
    print("所有卡片处理完毕。")

if __name__ == "__main__":
    main()
