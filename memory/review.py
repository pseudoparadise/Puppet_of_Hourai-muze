"""
review.py - 记忆卡片审核闸门
读取 pending_cards.json，逐条展示，由沐泽决定通过/拒绝。
"""
import json
import os
import sys
import sqlite3
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from encoder import embed, create_index, add_to_index, save_index

PENDING_PATH = os.path.join(os.path.dirname(__file__), "pending_cards.json")
DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")
INDEX_PATH = os.path.join(os.path.dirname(__file__), "vectors.faiss")

def load_pending():
    if not os.path.exists(PENDING_PATH):
        return []
    with open(PENDING_PATH, "r", encoding="utf-8") as f:
        return json.load(f)

def save_pending(pending_list):
    with open(PENDING_PATH, "w", encoding="utf-8") as f:
        json.dump(pending_list, f, ensure_ascii=False, indent=2)

def approve_card(card):
    """通过审核：写入数据库，生成向量，加入FAISS索引"""
    conn = sqlite3.connect(DB_PATH)
    try:
        # 使用 INSERT OR REPLACE 处理 ID 冲突
        conn.execute("""
            INSERT OR REPLACE INTO cards (id, title, content, keywords, embedding, importance, category, review_status, enabled_in_context)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'final', 1)
        """, (
            card["id"],
            card["title"],
            card["content"],
            card["keywords"],
            None,  # 向量稍后更新
            card.get("importance", 5),
            card.get("category", "interaction")
        ))
        conn.commit()

        # 生成向量
        vec = embed(card["content"])
        vec_bytes = vec.tobytes()
        conn.execute("UPDATE cards SET embedding = ? WHERE id = ?", (vec_bytes, card["id"]))
        conn.commit()

        # 加入 FAISS 索引
        index = create_index()
        add_to_index(index, card["id"], vec)
        save_index(index)
        print(f"  ✅ 已通过并入库: {card['id']}")
    except Exception as e:
        print(f"  ❌ 入库失败: {e}")
    finally:
        conn.close()

def reject_card(card):
    """拒绝审核：从列表中删除，不写入数据库"""
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

    # 清空 pending_cards.json（已全部处理）
    save_pending([])
    print("所有卡片处理完毕。")

if __name__ == "__main__":
    main()