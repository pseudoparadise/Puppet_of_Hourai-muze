"""
re-embed.py — 全量重建 embedding 向量
扫描所有 final 卡片，用 build_embed_text() 重新生成 2048 维向量，
写回 DB 并重建 FAISS 索引 + link graph。
用法：python -m memory.re_embed
"""
import os, sys, sqlite3, time
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from encoder import embed, load_index, add_to_index, save_index, build_embed_text

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")
BATCH_SIZE = 3
BATCH_PAUSE = 2


def main():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM cards WHERE review_status='final'")
    all_cards = [dict(r) for r in c.fetchall()]
    conn.close()

    if not all_cards:
        print("无卡片。")
        return

    index = load_index()

    # 清空旧 FAISS 索引
    import faiss
    DIM = 2048
    base = faiss.IndexFlatL2(DIM)
    index = faiss.IndexIDMap(base)

    success = 0
    fail = 0
    skipped = 0

    for i, card in enumerate(all_cards):
        cid = card["id"]
        cat = card.get("category", "")
        label = f"{cid}: {card['title'][:40]} [{cat}]"
        print(f"[{i+1}/{len(all_cards)}] {label}")

        if cat == "erotic":
            print(f"  ⏭  跳过 (erotic 类不动)")
            skipped += 1
            add_to_index(index, cid, np.frombuffer(card["embedding"], dtype=np.float32))
            continue

        try:
            text = build_embed_text(card)
            vec = embed(text)
            vec_bytes = vec.tobytes()

            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE cards SET embedding = ? WHERE id = ?", (vec_bytes, cid))
            conn.commit()
            conn.close()

            add_to_index(index, cid, vec)
            print(f"  ✅ 已重嵌 ({vec.shape[0]} 维)")
            success += 1
        except Exception as e:
            print(f"  ❌ 失败: {e}")
            # 保留旧向量
            if card.get("embedding"):
                add_to_index(index, cid, np.frombuffer(card["embedding"], dtype=np.float32))
            fail += 1

        if (i + 1) % BATCH_SIZE == 0 and i + 1 < len(all_cards):
            print(f"  ⏸  暂停 {BATCH_PAUSE}s ...")
            time.sleep(BATCH_PAUSE)

    save_index(index)
    print(f"\n完成: {success} 重嵌, {skipped} 跳过 (erotic), {fail} 失败, {len(all_cards)} 总计")

    try:
        from linker import rebuild_all_links as _rebuild
        _rebuild()
        print("✅ link graph 已重建")
    except Exception as _le:
        print(f"[link] 重建跳过: {_le}")


if __name__ == "__main__":
    main()
