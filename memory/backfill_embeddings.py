"""
backfill_embeddings.py — 批量标注老卡 embedding 向量
扫描 cards.db 中 embedding IS NULL 的卡片，调 embed() 生成 2048 维向量，
写回 DB 并更新 FAISS 索引。已有向量的自动跳过，可重复安全运行。
用法：python backfill_embeddings.py
"""
import os, sys, sqlite3, time
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from encoder import embed, load_index, add_to_index, save_index

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")
BATCH_SIZE = 3   # 每批处理张数
BATCH_PAUSE = 2  # 批次间暂停秒数，避免 API 限流


def main():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, title, content FROM cards WHERE embedding IS NULL")
    missing = c.fetchall()
    conn.close()

    if not missing:
        print("所有卡片已有向量，无需回填。")
        return

    print(f"找到 {len(missing)} 张缺向量的卡片，开始回填...\n")
    index = load_index()
    success = 0
    fail = 0

    for i, (card_id, title, content) in enumerate(missing):
        text = title + " " + (content or "")
        print(f"[{i+1}/{len(missing)}] {card_id}: {title[:50]}")

        try:
            vec = embed(text)
            vec_bytes = vec.tobytes()

            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("UPDATE cards SET embedding = ? WHERE id = ?", (vec_bytes, card_id))
            conn.commit()
            conn.close()

            add_to_index(index, card_id, vec)
            print(f"  ✅ 向量已写入 ({vec.shape[0]} 维)")
            success += 1
        except Exception as e:
            print(f"  ❌ 失败: {e}")
            fail += 1

        # 批次暂停
        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(missing):
            print(f"  ⏸  暂停 {BATCH_PAUSE}s ...")
            time.sleep(BATCH_PAUSE)

    save_index(index)
    print(f"\n完成: {success} 成功, {fail} 失败, {len(missing)} 总计")

    # 全量重建 link graph
    try:
        from linker import rebuild_all_links as _rebuild
        _rebuild()
    except Exception as _le:
        print(f"[link] 重建跳过: {_le}")

    # 验证
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM cards WHERE embedding IS NULL")
    remaining = c.fetchone()[0]
    conn.close()
    print(f"剩余缺向量: {remaining}")
    if remaining == 0:
        print("✅ 全部卡片向量就绪。")


if __name__ == "__main__":
    main()
