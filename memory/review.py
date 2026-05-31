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
from encoder import embed, load_index, add_to_index, save_index, build_embed_text

PENDING_PATH = os.path.join(os.path.dirname(__file__), "pending_cards.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

def load_pending():
    from shared import load_json_safe
    return load_json_safe(PENDING_PATH, default=[], label="review")

def save_pending(pending_list):
    from delegate_tools import atomic_write_json
    atomic_write_json(PENDING_PATH, pending_list)

def approve_card(card):
    """通过审核：写入数据库，生成向量，加入FAISS索引"""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute("""
            INSERT OR REPLACE INTO cards (id, title, content, keywords, embedding, importance, category, review_status, enabled_in_context, chord, valence, arousal, target_date, user_raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'final', 1, ?, ?, ?, ?, ?)
        """, (
            card["id"], card["title"], card["content"], card["keywords"],
            None, card.get("importance", 5), card.get("category", "interaction"),
            card.get("chord") or "", card.get("valence", 0.0), card.get("arousal", 0.5),
            card.get("target_date"), card.get("user_raw", "")
        ))
        conn.commit()

        # 复用 pending 预计算向量，避免重复调豆包 API
        pre_vec = card.get("_embed_vec")
        if pre_vec is not None:
            vec = np.array(pre_vec, dtype=np.float32)
            print(f"  📎 复用预计算向量 ({vec.shape[0]} 维)")
        else:
            vec = embed(build_embed_text(card))
        vec_bytes = vec.tobytes()
        conn.execute("UPDATE cards SET embedding = ? WHERE id = ?", (vec_bytes, card["id"]))
        conn.commit()

        # ── FIX: 不再 create_index() 覆盖！改为 load→add→save ──
        index = load_index()
        add_to_index(index, card["id"], vec)
        save_index(index)
        print(f"  ✅ 已通过并入库: {card['id']}")
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
