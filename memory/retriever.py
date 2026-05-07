"""
retriever.py - 双路径检索 + 重排 Top 3
根据导演后期要求，统一使用 encoder 提供的 load_index 接口。
"""
import sqlite3
import os
from encoder import embed, load_index, search_index, DIM
import numpy as np

def retrieve(query: str, db_path: str = None, top_k: int = 3) -> list:
    if db_path is None:
        db_path = os.path.join(os.path.dirname(__file__), "cards.db")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    # 1. 关键词召回
    c.execute("SELECT id, keywords, importance, category, content, title, created_at, last_referenced_at FROM cards WHERE review_status='final' AND enabled_in_context=1")
    all_cards = [dict(row) for row in c.fetchall()]

    query_lower = query.lower()
    keyword_hits = []
    for card in all_cards:
        kws = [kw.strip().lower() for kw in card["keywords"].split(",") if kw.strip()]
        hit_count = sum(1 for kw in kws if kw in query_lower)
        if hit_count > 0:
            card["hit_count"] = hit_count
            card["distance"] = 1.0
            
            # ── v3 打分骨架 ──
            w_keyword = 0.0
            w_semantic = 1.0
            w_importance = 0.5
            anchor_bonus = 0.0
            growth_bonus = 0.0
            time_burst = 0.0
            decay_penalty = 0.0
            
            dist_sigmoid = 1.0
            keyword_score = hit_count * w_keyword
            semantic_score = (1.0 - dist_sigmoid) * w_semantic
            importance_score = card["importance"] * w_importance

            card["score"] = (
                keyword_score + semantic_score + importance_score +
                anchor_bonus + growth_bonus + time_burst - decay_penalty
            )
            
            keyword_hits.append(card)

    # 2. 语义召回
    semantic_hits = []
    if len(keyword_hits) < top_k:
        try:
            query_vec = embed(query)
            index = load_index()
            if index.ntotal > 0:
                candidates = search_index(index, query_vec, k=10)
                keyword_ids = {c["id"] for c in keyword_hits}
                for cid, dist in candidates:
                    if cid in keyword_ids:
                        continue
                    c.execute("SELECT id, keywords, importance, category, content, title, created_at, last_referenced_at FROM cards WHERE id=? AND review_status='final' AND enabled_in_context=1", (cid,))
                    row = c.fetchone()
                    if row:
                        card = dict(row)
                        card["hit_count"] = 0
                        card["distance"] = dist
                        
                        dist_sigmoid = 2.0 / (1.0 + np.exp(dist))
                        
                        # ── v3 打分骨架 ──
                        w_keyword = 0.0
                        w_semantic = 1.0
                        w_importance = 0.5
                        anchor_bonus = 0.0
                        growth_bonus = 0.0
                        time_burst = 0.0
                        decay_penalty = 0.0

                        keyword_score = hit_count * w_keyword
                        semantic_score = (1.0 - dist_sigmoid) * w_semantic
                        importance_score = card["importance"] * w_importance

                        card["score"] = (
                            keyword_score + semantic_score + importance_score +
                            anchor_bonus + growth_bonus + time_burst - decay_penalty
                        )
                        
                        semantic_hits.append(card)
        except Exception as e:
            print(f"[语义召回异常]: {e}")

    conn.close()

    seen = {c["id"] for c in keyword_hits}
    merged = list(keyword_hits)
    for c in semantic_hits:
        if c["id"] not in seen:
            merged.append(c)
            seen.add(c["id"])

    merged.sort(key=lambda x: x["score"], reverse=True)

    result = []
    categories_used = set()
    for card in merged:
        if len(result) >= top_k:
            break
        if len(result) > 0 and len(categories_used) < 2 and card["category"] in categories_used and len(result) == top_k - 1:
            for later in merged[len(result):]:
                if later["category"] not in categories_used:
                    result.append(later)
                    categories_used.add(later["category"])
                    break
        result.append(card)
        categories_used.add(card["category"])

    result = result[:top_k]

    output = []
    for card in result:
        output.append({
            "id": card["id"],
            "title": card["title"],
            "content": card["content"],
            "keywords": card["keywords"],
            "importance": card["importance"],
            "category": card["category"],
            "score": card["score"],
            "hit_count": card.get("hit_count", 0),
            "distance": card.get("distance", 1.0)
        })
    return output