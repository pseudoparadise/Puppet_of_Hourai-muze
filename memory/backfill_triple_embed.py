"""
backfill_triple_embed.py — 一次性存量回填三向量
为所有已有 embedding 的卡片生成 embedding_kw + embedding_quote。
FAISS 索引不需要重建（摘要向量没变）。
"""
import sqlite3
import os
import sys
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from encoder import (
    embed, build_embed_summary, build_embed_kw, build_embed_quote,
    load_index, add_to_index, save_index
)

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")


def backfill(dry_run=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 查出需要回填的卡片（有旧 embedding 但缺新的）
    c.execute("""
        SELECT id, title, content, keywords, user_raw, category
        FROM cards
        WHERE review_status = 'final'
        AND embedding IS NOT NULL
        AND (embedding_kw IS NULL OR embedding_quote IS NULL)
    """)
    rows = c.fetchall()
    total = len(rows)
    print(f"待回填: {total} 张卡片")

    if total == 0:
        print("无需回填。")
        conn.close()
        return

    success = 0
    fail = 0

    for i, row in enumerate(rows):
        card = dict(row)
        cid = card["id"]
        try:
            # 生成三个向量
            text_summary = build_embed_summary(card)
            text_kw = build_embed_kw(card)
            text_quote = build_embed_quote(card)

            if dry_run:
                print(f"[{i+1}/{total}] DRY RUN {cid}")
                print(f"  summary: {text_summary[:80]}...")
                print(f"  kw:      {text_kw[:80]}...")
                print(f"  quote:   {text_quote[:80]}...")
            else:
                vec_summary = embed(text_summary) if text_summary else None
                vec_kw = embed(text_kw) if text_kw else None
                vec_quote = embed(text_quote) if text_quote else None

                # 写 DB
                c.execute(
                    "UPDATE cards SET embedding=?, embedding_kw=?, embedding_quote=? WHERE id=?",
                    (
                        vec_summary.tobytes() if vec_summary is not None else None,
                        vec_kw.tobytes() if vec_kw is not None else None,
                        vec_quote.tobytes() if vec_quote is not None else None,
                        cid,
                    )
                )

                # 更新 FAISS（摘要向量可能和旧的不同——旧的是糅合向量）
                if vec_summary is not None:
                    index = load_index()
                    add_to_index(index, cid, vec_summary)

                success += 1
                if (i + 1) % 10 == 0 or i == total - 1:
                    print(f"[{i+1}/{total}] {cid} OK ({success} 成功, {fail} 失败)")
                    if vec_summary is not None and (i + 1) % 10 == 0:
                        save_index(index)

        except Exception as e:
            fail += 1
            print(f"[{i+1}/{total}] {cid} FAILED: {e}")

    if not dry_run:
        conn.commit()
        save_index(index)
        print(f"\n回填完成: {success} 成功, {fail} 失败")
    else:
        print(f"\n[dry run] 共 {total} 张待回填")

    conn.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    backfill(dry_run=args.dry_run)
