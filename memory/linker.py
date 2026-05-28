"""
linker.py — 卡片 link graph

新卡入库时自动建 link：embed → FAISS 最近邻 → cosine ≥ 阈值 → 写入 card_links
召回时一阶扩散：沿 link 边走到邻居，衰减权重加入候选池
"""
import sqlite3
import os
import numpy as np
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")
LINK_THRESHOLD = 0.85
LINK_K = 10  # FAISS 搜索的最近邻数量

# 分类防火墙：这些分类对之间不建 link 边
LINK_CATEGORY_BLOCK = {
    ('deep_talks', 'erotic'), ('erotic', 'deep_talks'),
    ('milestone', 'erotic'), ('erotic', 'milestone'),
    ('turning_points', 'erotic'), ('erotic', 'turning_points'),
    ('daily_life', 'erotic'), ('erotic', 'daily_life'),
}


def _categories_compatible(cat_a: str, cat_b: str) -> bool:
    return (cat_a, cat_b) not in LINK_CATEGORY_BLOCK


def ensure_link_table():
    """创建 card_links 表（幂等）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS card_links (
            card_id_a TEXT,
            card_id_b TEXT,
            similarity REAL NOT NULL,
            relation TEXT NOT NULL DEFAULT 'link',
            created_at TEXT NOT NULL,
            PRIMARY KEY (card_id_a, card_id_b)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_links_a ON card_links(card_id_a)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_links_b ON card_links(card_id_b)
    """)
    conn.commit()
    conn.close()


def _normalize_pair(a: str, b: str) -> tuple:
    """保证 a < b，无向边只存一行。"""
    return (a, b) if a < b else (b, a)


def build_links(card_id: str, vec: np.ndarray):
    """为新卡片寻找语义近邻并写入 link 边。vec 是 (2048,) float32 embedding。"""
    ensure_link_table()

    try:
        from .encoder import load_index
        index = load_index()
        if index.ntotal < 2:
            return 0
    except Exception:
        return 0

    neighbors = index.search(vec.reshape(1, -1).astype(np.float32), LINK_K + 1)
    # neighbors 返回 (distances, indices)，indices 是 FAISS 内部 int id
    faiss_indices = neighbors[1][0]
    faiss_distances = neighbors[0][0]

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # FAISS int id → card str id
    from .encoder import _int_id_to_str
    created = 0
    now = datetime.now().isoformat()

    for faiss_id, l2_dist in zip(faiss_indices, faiss_distances):
        other_id = _int_id_to_str(int(faiss_id))
        if not other_id or other_id == card_id:
            continue

        # L2 距离转 cosine 相似度（向量已归一化时成立；豆包输出接近归一化）
        cosine = float(1.0 - l2_dist ** 2 / 2.0)
        cosine = max(0.0, min(1.0, cosine))

        if cosine < LINK_THRESHOLD:
            continue

        # 分类防火墙
        _cat_a = conn.execute("SELECT category FROM cards WHERE id=?", (card_id,)).fetchone()
        _cat_b = conn.execute("SELECT category FROM cards WHERE id=?", (other_id,)).fetchone()
        if _cat_a and _cat_b and not _categories_compatible(_cat_a[0], _cat_b[0]):
            continue

        a, b = _normalize_pair(card_id, other_id)
        conn.execute(
            "INSERT OR IGNORE INTO card_links (card_id_a, card_id_b, similarity, relation, created_at) "
            "VALUES (?, ?, ?, 'link', ?)",
            (a, b, round(cosine, 4), now)
        )
        if conn.total_changes > 0:
            created += 1
            print(f"[link] {card_id} ↔ {other_id} (cos={cosine:.3f})")

    conn.commit()
    conn.close()
    return created


def get_linked_cards(card_id: str) -> list:
    """返回 card_id 的所有 link 邻居 ID 列表。"""
    ensure_link_table()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT card_id_b FROM card_links WHERE card_id_a=? "
        "UNION ALL SELECT card_id_a FROM card_links WHERE card_id_b=?",
        (card_id, card_id)
    ).fetchall()
    conn.close()
    return [r[0] for r in rows]


def get_linked_with_similarity(card_id: str) -> list:
    """返回 [(neighbor_id, similarity), ...] 按相似度降序。"""
    ensure_link_table()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT card_id_b, similarity FROM card_links WHERE card_id_a=? "
        "UNION ALL SELECT card_id_a, similarity FROM card_links WHERE card_id_b=? "
        "ORDER BY similarity DESC",
        (card_id, card_id)
    ).fetchall()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def remove_links(card_id: str):
    """删除一张卡的所有 link 边（卡片被删除时调用）。"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM card_links WHERE card_id_a=? OR card_id_b=?", (card_id, card_id))
    conn.commit()
    conn.close()


def rebuild_all_links():
    """全量重建所有卡片的 link graph。用于 backfill 或阈值调整后。"""
    ensure_link_table()
    from .encoder import load_index, _int_id_to_str

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 获取所有有 embedding 的 final 卡片
    rows = conn.execute(
        "SELECT id, embedding FROM cards WHERE review_status='final' AND embedding IS NOT NULL"
    ).fetchall()

    index = load_index()
    if index.ntotal < 2:
        conn.close()
        return 0

    conn.execute("DELETE FROM card_links")
    total = 0
    now = datetime.now().isoformat()

    for row in rows:
        card_id = row["id"]
        vec = np.frombuffer(row["embedding"], dtype=np.float32)

        neighbors = index.search(vec.reshape(1, -1).astype(np.float32), LINK_K + 1)
        faiss_indices = neighbors[1][0]
        faiss_distances = neighbors[0][0]

        for faiss_id, l2_dist in zip(faiss_indices, faiss_distances):
            other_id = _int_id_to_str(int(faiss_id))
            if not other_id or other_id == card_id:
                continue

            cosine = float(1.0 - l2_dist ** 2 / 2.0)
            cosine = max(0.0, min(1.0, cosine))

            if cosine < LINK_THRESHOLD:
                continue

            # 分类防火墙
            _cat_a = conn.execute("SELECT category FROM cards WHERE id=?", (card_id,)).fetchone()
            _cat_b = conn.execute("SELECT category FROM cards WHERE id=?", (other_id,)).fetchone()
            if _cat_a and _cat_b and not _categories_compatible(_cat_a[0], _cat_b[0]):
                continue

            a, b = _normalize_pair(card_id, other_id)
            conn.execute(
                "INSERT OR IGNORE INTO card_links (card_id_a, card_id_b, similarity, relation, created_at) "
                "VALUES (?, ?, ?, 'link', ?)",
                (a, b, round(cosine, 4), now)
            )
            total += 1

    conn.commit()
    conn.close()
    print(f"[link] 全量重建完成: {total} 条边")
    return total
