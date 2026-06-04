"""
linker.py — 卡片 link graph（多维关联 + 因果方向版）

人类记忆不是单维语义——想起中学时代的一张卡，会沿时间线和因果链扩散。
link 打分由四维组成：
  1. 语义相似 (cosine) — FAISS embedding
  2. 时间邻近 — target_date 越近分越高
  3. 分类同属 — 同 category 加分
  4. 关键词交集 — Jaccard 相似度

方向边自动推断：早→晚 = precedes，同季 = accompanies。
传递闭包补边：A→B + B→C 存在时，A→C 降低阈值尝试建边。

召回时一阶扩散：沿 link 边走到邻居，precedes 边全权，accompanies 0.8x，
衰减权重加入候选池。
"""
import sqlite3
import os
import numpy as np
from datetime import datetime, timedelta

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")
COMPOSITE_THRESHOLD = 0.50
TRANSITIVE_BONUS = 0.05  # 传递闭包降低的阈值
LINK_K = 12
MAX_LINKS_PER_CARD = 10

LINK_CATEGORY_BLOCK = {
    ('deep_talks', 'erotic'), ('erotic', 'deep_talks'),
    ('milestone', 'erotic'), ('erotic', 'milestone'),
    ('turning_points', 'erotic'), ('erotic', 'turning_points'),
    ('daily_life', 'erotic'), ('erotic', 'daily_life'),
}


def _categories_compatible(cat_a: str, cat_b: str) -> bool:
    return (cat_a, cat_b) not in LINK_CATEGORY_BLOCK


def ensure_link_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS card_links (
            card_id_a TEXT,
            card_id_b TEXT,
            similarity REAL NOT NULL,
            relation TEXT NOT NULL DEFAULT 'link',
            direction TEXT NOT NULL DEFAULT 'unknown',
            manual INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (card_id_a, card_id_b)
        )
    """)
    for col, col_type in [('direction', 'TEXT NOT NULL DEFAULT "unknown"'),
                          ('manual', 'INTEGER NOT NULL DEFAULT 0')]:
        try:
            conn.execute(f"ALTER TABLE card_links ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    conn.execute("CREATE INDEX IF NOT EXISTS idx_links_a ON card_links(card_id_a)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_links_b ON card_links(card_id_b)")
    conn.commit()
    conn.close()


def _normalize_pair(a: str, b: str) -> tuple:
    return (a, b) if a < b else (b, a)


def _parse_date(date_str: str):
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y-%m"):
        try:
            return datetime.strptime(date_str.strip(), fmt)
        except ValueError:
            continue
    return None


def _temporal_score(date_a: str, date_b: str) -> float:
    """时间邻近打分：同季→0.30，同年→0.22，邻年→0.15，2-5年→0.12，远→0.02。"""
    da = _parse_date(date_a)
    db = _parse_date(date_b)
    if not da or not db:
        return 0.0
    days = abs((da - db).days)
    if days <= 90:
        return 0.30
    elif days <= 365:
        return 0.22
    elif days <= 730:
        return 0.15
    elif days <= 1825:
        return 0.12
    else:
        return 0.02


def _infer_direction(date_a: str, date_b: str) -> str:
    """推断因果方向（a/b 为 _normalize_pair 排序后的顺序）。
    'forward' = a 在 b 之前，'backward' = b 在 a 之前，
    'parallel' = 同季无法区分，'unknown' = 缺日期。"""
    da = _parse_date(date_a)
    db = _parse_date(date_b)
    if not da or not db:
        return 'unknown'
    diff = (db - da).days
    if abs(diff) <= 90:
        return 'parallel'
    elif diff > 0:
        return 'forward'
    else:
        return 'backward'


def _keyword_jaccard(kw_a: str, kw_b: str) -> float:
    if not kw_a or not kw_b:
        return 0.0
    set_a = set(w.strip() for w in kw_a.replace(",", " ").replace("，", " ").split() if w.strip())
    set_b = set(w.strip() for w in kw_b.replace(",", " ").replace("，", " ").split() if w.strip())
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def _compute_link_score(cosine: float, card_a: dict, card_b: dict) -> tuple:
    """返回 (score, relation_label, direction)。"""
    score = 0.0
    drivers = []

    # 1. 语义基底 — 权重 0.40
    sem_contrib = cosine * 0.40
    score += sem_contrib
    if cosine >= 0.60:
        drivers.append("sem")

    # 2. 时间邻近 — 权重 0.30
    temporal = _temporal_score(
        card_a.get("target_date", ""),
        card_b.get("target_date", ""),
    )
    score += temporal
    if temporal >= 0.15:
        drivers.append("time")

    # 3. 分类同属 — 权重 0.15
    cat_a = card_a.get("category", "")
    cat_b = card_b.get("category", "")
    cat_match = 0.15 if (cat_a and cat_b and cat_a == cat_b) else 0.0
    score += cat_match
    if cat_match:
        drivers.append("cat")

    # 4. 关键词交集 — 权重 0.15
    jaccard = _keyword_jaccard(
        card_a.get("keywords", "") or "",
        card_b.get("keywords", "") or "",
    )
    kw_contrib = jaccard * 0.15
    score += kw_contrib
    if jaccard >= 0.2:
        drivers.append("kw")

    relation = "+".join(drivers) if drivers else "sem"
    direction = _infer_direction(
        card_a.get("target_date", ""),
        card_b.get("target_date", ""),
    )
    return min(1.0, score), relation, direction


def _insert_link(conn, card_id_a, card_id_b, score, relation, direction, now, manual=0):
    a, b = _normalize_pair(card_id_a, card_id_b)
    conn.execute(
        "INSERT OR IGNORE INTO card_links "
        "(card_id_a, card_id_b, similarity, relation, direction, manual, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (a, b, round(score, 4), relation, direction, manual, now)
    )
    return conn.total_changes


def _apply_transitive_closure(conn, all_cards, now):
    """传递闭包：如果 A-B 和 B-C 都是 precedes/accompanies 边，
    则为 A-C 降低阈值尝试建边。"""
    edges = conn.execute(
        "SELECT card_id_a, card_id_b, similarity, direction FROM card_links"
    ).fetchall()

    adjacency = {}
    for a, b, sim, direction in edges:
        adjacency.setdefault(a, {})[b] = (sim, direction)
        adjacency.setdefault(b, {})[a] = (sim, direction)

    new_edges = 0
    checked = set()
    for node_a in list(adjacency.keys()):
        for node_b in list(adjacency.get(node_a, {}).keys()):
            for node_c in list(adjacency.get(node_b, {}).keys()):
                if node_c == node_a:
                    continue
                # 确保 A-C 还没边
                if node_c in adjacency.get(node_a, {}):
                    continue
                pair = (min(node_a, node_c), max(node_a, node_c))
                if pair in checked:
                    continue
                checked.add(pair)

                card_a = all_cards.get(node_a)
                card_c = all_cards.get(node_c)
                if not card_a or not card_c:
                    continue
                if not _categories_compatible(
                    card_a.get("category", ""), card_c.get("category", "")
                ):
                    continue

                # 用 embedding 算 cosine
                va = np.frombuffer(card_a["embedding"], dtype=np.float32)
                vc = np.frombuffer(card_c["embedding"], dtype=np.float32)
                cosine = float(np.dot(va, vc))

                score, relation, direction = _compute_link_score(cosine, card_a, card_c)

                # 传递闭包：降低阈值
                if score >= COMPOSITE_THRESHOLD - TRANSITIVE_BONUS:
                    relation += "+chain"
                    if _insert_link(conn, node_a, node_c, score, relation, direction, now):
                        new_edges += 1
                        adjacency.setdefault(node_a, {})[node_c] = (score, direction)
                        adjacency.setdefault(node_c, {})[node_a] = (score, direction)
                        print(f"[link] 传递补边: {card_a['title']} <-> {card_c['title']} "
                              f"(score={score:.3f} cos={cosine:.3f} {relation})")

    return new_edges


def build_links(card_id: str, vec: np.ndarray):
    """为新卡片寻找多维关联近邻并写入 link 边。"""
    ensure_link_table()

    try:
        from encoder import load_index
        index = load_index()
        if index.ntotal < 2:
            return 0
    except Exception:
        import traceback
        traceback.print_exc()
        return 0

    neighbors = index.search(vec.reshape(1, -1).astype(np.float32), LINK_K + 1)
    faiss_indices = neighbors[1][0]
    faiss_distances = neighbors[0][0]

    from encoder import _int_id_to_str

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    card_row = conn.execute(
        "SELECT id, title, category, keywords, target_date FROM cards WHERE id=?",
        (card_id,)
    ).fetchone()
    if not card_row:
        conn.close()
        return 0
    card_a = dict(card_row)

    # 检查已有链接数，不超过上限
    existing_count = conn.execute(
        "SELECT COUNT(*) FROM card_links WHERE card_id_a=? OR card_id_b=?", (card_id, card_id)
    ).fetchone()[0]
    remaining = max(0, MAX_LINKS_PER_CARD - existing_count)
    created = 0
    now = datetime.now().isoformat()

    for faiss_id, l2_dist in zip(faiss_indices, faiss_distances):
        if remaining <= 0:
            break
        other_id = _int_id_to_str(int(faiss_id))
        if not other_id or other_id == card_id:
            continue

        other_row = conn.execute(
            "SELECT id, title, category, keywords, target_date FROM cards WHERE id=?",
            (other_id,)
        ).fetchone()
        if not other_row:
            continue
        card_b = dict(other_row)

        # 对方也已满则跳过
        other_count = conn.execute(
            "SELECT COUNT(*) FROM card_links WHERE card_id_a=? OR card_id_b=?", (other_id, other_id)
        ).fetchone()[0]
        if other_count >= MAX_LINKS_PER_CARD:
            continue

        cosine = float(1.0 - l2_dist ** 2 / 2.0)
        cosine = max(0.0, min(1.0, cosine))

        if not _categories_compatible(card_a["category"], card_b["category"]):
            continue

        score, relation, direction = _compute_link_score(cosine, card_a, card_b)

        if score < COMPOSITE_THRESHOLD:
            continue

        if _insert_link(conn, card_id, other_id, score, relation, direction, now):
            created += 1
            remaining -= 1
            print(f"[link] {card_a['title']} <-> {card_b['title']} "
                  f"(score={score:.3f} cos={cosine:.3f} {relation} {direction})")

    # 传递闭包：扫描新卡是否构成新的 A→新卡→C 或 A→新卡 桥接
    all_rows = conn.execute(
        "SELECT id, embedding, title, category, keywords, target_date "
        "FROM cards WHERE review_status='final' AND embedding IS NOT NULL"
    ).fetchall()
    all_cards = {r["id"]: dict(r) for r in all_rows}
    chain_created = _apply_transitive_closure(conn, all_cards, now)

    conn.commit()
    conn.close()
    return created + chain_created


def get_linked_cards(card_id: str) -> list:
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
    """返回 [(neighbor_id, similarity, direction, manual), ...] 按相似度降序。"""
    ensure_link_table()
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT card_id_b, similarity, direction, manual FROM card_links WHERE card_id_a=? "
        "UNION ALL SELECT card_id_a, similarity, "
        "CASE direction WHEN 'forward' THEN 'backward' WHEN 'backward' THEN 'forward' ELSE direction END, "
        "manual "
        "FROM card_links WHERE card_id_b=? "
        "ORDER BY similarity DESC",
        (card_id, card_id)
    ).fetchall()
    conn.close()
    return [(r[0], r[1], r[2], bool(r[3])) for r in rows]


def remove_links(card_id: str, include_manual: bool = False):
    conn = sqlite3.connect(DB_PATH)
    if include_manual:
        conn.execute("DELETE FROM card_links WHERE card_id_a=? OR card_id_b=?", (card_id, card_id))
    else:
        conn.execute("DELETE FROM card_links WHERE (card_id_a=? OR card_id_b=?) AND manual=0", (card_id, card_id))
    conn.commit()
    conn.close()


def create_manual_link(card_id_a: str, card_id_b: str, relation: str = 'manual:causal') -> bool:
    """手动创建因果边。不受分类防火墙限制，rebulid 不删除。"""
    ensure_link_table()
    conn = sqlite3.connect(DB_PATH)

    # 取日期推断方向
    rows = conn.execute(
        "SELECT id, target_date FROM cards WHERE id IN (?, ?)", (card_id_a, card_id_b)
    ).fetchall()
    dates = {r[0]: r[1] for r in rows}

    a, b = _normalize_pair(card_id_a, card_id_b)
    direction = _infer_direction(dates.get(a, ''), dates.get(b, ''))

    now = datetime.now().isoformat()
    conn.execute(
        "INSERT OR REPLACE INTO card_links "
        "(card_id_a, card_id_b, similarity, relation, direction, manual, created_at) "
        "VALUES (?, ?, 0.999, ?, ?, 1, ?)",
        (a, b, relation, direction, now)
    )
    conn.commit()
    conn.close()
    print(f"[link] manual: {card_id_a} <-> {card_id_b} ({relation} {direction})")
    return True


def break_link(card_id_a: str, card_id_b: str) -> bool:
    """断开两张卡之间的 link 边（含手动边）。"""
    conn = sqlite3.connect(DB_PATH)
    a, b = _normalize_pair(card_id_a, card_id_b)
    conn.execute("DELETE FROM card_links WHERE card_id_a=? AND card_id_b=?", (a, b))
    conn.commit()
    affected = conn.total_changes
    conn.close()
    if affected:
        print(f"[link] 断连: {card_id_a} <-> {card_id_b}")
    return affected > 0


def rebuild_all_links():
    """全量重建所有卡片的 link graph（含传递闭包 + 方向边）。"""
    ensure_link_table()
    from encoder import load_index, _int_id_to_str

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT id, embedding, title, category, keywords, target_date "
        "FROM cards WHERE review_status='final' AND embedding IS NOT NULL"
    ).fetchall()

    all_cards = {}
    for row in rows:
        all_cards[row["id"]] = dict(row)

    index = load_index()
    if index.ntotal < 2:
        conn.close()
        return 0

    conn.execute("DELETE FROM card_links WHERE manual=0")
    total = conn.execute("SELECT COUNT(*) FROM card_links WHERE manual=1").fetchone()[0]
    now = datetime.now().isoformat()

    # 第一遍：直接建边（每卡最多 MAX_LINKS_PER_CARD 条）
    per_card_count = {cid: 0 for cid in all_cards}
    for card_id, card_a in all_cards.items():
        if per_card_count[card_id] >= MAX_LINKS_PER_CARD:
            continue
        vec = np.frombuffer(card_a["embedding"], dtype=np.float32)

        neighbors = index.search(vec.reshape(1, -1).astype(np.float32), LINK_K + 1)
        faiss_indices = neighbors[1][0]
        faiss_distances = neighbors[0][0]

        for faiss_id, l2_dist in zip(faiss_indices, faiss_distances):
            other_id = _int_id_to_str(int(faiss_id))
            if not other_id or other_id == card_id:
                continue
            if per_card_count[card_id] >= MAX_LINKS_PER_CARD:
                break
            if per_card_count.get(other_id, 0) >= MAX_LINKS_PER_CARD:
                continue
            card_b = all_cards.get(other_id)
            if not card_b:
                continue

            cosine = float(1.0 - l2_dist ** 2 / 2.0)
            cosine = max(0.0, min(1.0, cosine))

            if not _categories_compatible(card_a.get("category", ""), card_b.get("category", "")):
                continue

            score, relation, direction = _compute_link_score(cosine, card_a, card_b)

            if score < COMPOSITE_THRESHOLD:
                continue

            if _insert_link(conn, card_id, other_id, score, relation, direction, now):
                total += 1
                per_card_count[card_id] += 1
                per_card_count[other_id] = per_card_count.get(other_id, 0) + 1

    # 第二遍：传递闭包补边
    chain_edges = _apply_transitive_closure(conn, all_cards, now)
    total += chain_edges

    conn.commit()
    conn.close()
    print(f"[link] 全量重建完成: {total} 条边 (阈值={COMPOSITE_THRESHOLD}, 传递+{chain_edges})")
    return total
