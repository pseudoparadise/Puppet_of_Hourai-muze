"""
memory_manager.py - 记忆卡片全生命周期管理
整合续命、活跃状态、审计建议（去重、合并）、时间衰减
"""
import sqlite3
import os
import json
from datetime import datetime, timedelta, timezone
import numpy as np

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

# ── 1. 续命 ──

def renew_card(card_id: str) -> bool:
    """
    续命：更新卡片的 last_referenced_at 并令 usage_count += 1。
    返回 True 表示成功，False 表示卡片不存在或更新失败。
    """
    # 1. 清洗 ID：去掉可能残存的 "card:" 前缀和无关字符
    clean_id = card_id.strip()
    if ':' in clean_id:
        # 保留冒号后的真实 ID (例如 "card:3" -> "3")
        clean_id = clean_id.split(':')[-1].strip()
    
    # 2. 如果清洗后是空字符串，直接拒绝
    if not clean_id:
        print(f"[memory_manager] 续命失败：无效ID格式 ({card_id})")
        return False
        
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        now_utc = datetime.now(timezone.utc)
        now = now_utc.isoformat()
        c.execute(
            "UPDATE cards SET last_referenced_at = ?, usage_count = usage_count + 1 WHERE id = ?",
            (now, clean_id)
        )
        if c.rowcount == 0:
            return False
        conn.commit()
        return True
    except Exception as e:
        print(f"[memory_manager] 续命失败 card_id={card_id}: {e}")
        return False
    finally:
        conn.close()

# ── 2. 活跃状态管理 ──

def update_active_status():
    """
    定期调用（每天一次或每次对话前）：
    - 永久活跃豁免：milestone / commitments / deep_talks / importance >= 8
    - 30天滚动活跃：最近30天创建或被引用过
    - 不满足条件则 enabled_in_context = 0
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        now_utc = datetime.now(timezone.utc)
        c.execute("""
            UPDATE cards SET enabled_in_context = 0
            WHERE review_status = 'final'
        """)
        c.execute("""
            UPDATE cards SET enabled_in_context = 1
            WHERE review_status = 'final'
            AND (
                category IN ('milestone', 'commitments', 'deep_talks')
                OR importance >= 8
                OR (julianday(?) - julianday(created_at || '+00:00')) <= 30
                OR (julianday(?) - julianday(last_referenced_at || '+00:00')) <= 30
            )
        """, (now_utc.isoformat(), now_utc.isoformat()))
        conn.commit()
        print(f"[memory_manager] 活跃状态已更新，当前时间: {now_utc.isoformat()}")
    except Exception as e:
        print(f"[memory_manager] 活跃状态更新失败: {e}")
    finally:
        conn.close()

# ── 3. 审计建议：去重检测 ──

def check_duplicates(new_content: str, threshold: float = 0.85) -> list:
    """
    检测新卡片内容是否与现有 final 卡片高度重复。
    使用余弦相似度比较。
    返回重复卡片 ID 列表。
    """
    try:
        from encoder import embed, load_index, search_index
    except ImportError:
        print("[memory_manager] 无法导入 encoder，跳过去重检测")
        return []

    try:
        new_vec = embed(new_content)
        index = load_index()
        if index.ntotal == 0:
            return []

        # 搜索最相似的5张卡片
        candidates = search_index(index, new_vec, k=5)
        duplicates = []
        for card_id, distance in candidates:
            # 将 L2 距离转换为余弦相似度近似值
            similarity = 1.0 / (1.0 + distance)
            if similarity >= threshold:
                duplicates.append(card_id)

        if duplicates:
            print(f"[memory_manager] 去重检测：发现 {len(duplicates)} 张相似卡片 {duplicates}")
        return duplicates
    except Exception as e:
        print(f"[memory_manager] 去重检测异常: {e}")
        return []

# ── 4. 审计建议：importance 自动校准建议 ──

def suggest_importance_calibration():
    """
    检查 usage_count 与 importance 不匹配的卡片，输出建议日志。
    - usage_count >= 10 但 importance < 7 → 建议提升
    - importance >= 8 但 usage_count = 0 → 建议审视
    不做自动修改，只输出建议。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT id, title, importance, usage_count FROM cards WHERE review_status='final'")
        rows = c.fetchall()
        suggestions = []
        for row in rows:
            card_id, title, importance, usage_count = row
            if usage_count >= 10 and importance < 7:
                suggestions.append({
                    "card_id": card_id,
                    "title": title,
                    "current_importance": importance,
                    "usage_count": usage_count,
                    "suggestion": "建议提升 importance",
                    "reason": f"被引用了 {usage_count} 次，但 importance 仅 {importance}"
                })
            elif importance >= 8 and usage_count == 0:
                suggestions.append({
                    "card_id": card_id,
                    "title": title,
                    "current_importance": importance,
                    "usage_count": usage_count,
                    "suggestion": "建议审视 importance 是否虚高",
                    "reason": f"importance={importance} 但从未被引用"
                })

        if suggestions:
            print(f"[memory_manager] 发现 {len(suggestions)} 条重要性校准建议：")
            for s in suggestions:
                print(f"  {s['card_id']}: {s['suggestion']} ({s['reason']})")
        return suggestions
    except Exception as e:
        print(f"[memory_manager] importance 校准建议异常: {e}")
        return []
    finally:
        conn.close()

# ── 5. 审计建议：合并建议 ──

def suggest_merges():
    """
    检测语义相似度极高（>0.95）的卡片对，建议合并。
    """
    try:
        from encoder import load_index, search_index, embed
    except ImportError:
        print("[memory_manager] 无法导入 encoder，跳过合并建议")
        return []

    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT id, title, content FROM cards WHERE review_status='final' AND enabled_in_context=1")
        rows = c.fetchall()

        if len(rows) < 2:
            return []

        index = load_index()
        merge_suggestions = []
        checked = set()

        for row in rows:
            card_id, title, content = row
            if card_id in checked:
                continue
            try:
                vec = embed(content)
                similar = search_index(index, vec, k=5)
                for other_id, distance in similar:
                    if other_id == card_id or other_id in checked:
                        continue
                    similarity = 1.0 / (1.0 + distance)
                    if similarity > 0.95:
                        merge_suggestions.append({
                            "card_a": card_id,
                            "card_b": other_id,
                            "similarity": round(similarity, 3),
                            "suggestion": "建议合并"
                        })
                        checked.add(card_id)
                        checked.add(other_id)
                        break
            except Exception:
                continue

        if merge_suggestions:
            print(f"[memory_manager] 发现 {len(merge_suggestions)} 条合并建议：")
            for m in merge_suggestions:
                print(f"  {m['card_a']} <-> {m['card_b']} (相似度: {m['similarity']})")
        return merge_suggestions
    except Exception as e:
        print(f"[memory_manager] 合并建议异常: {e}")
        return []
    finally:
        conn.close()

# ── 6. 一键审计入口 ──

def run_audit():
    """
    执行完整审计流程：活跃状态更新 → 去重检测(基于近期pending) → importance校准建议 → 合并建议
    """
    print("[memory_manager] 开始执行完整审计...")
    update_active_status()
    suggest_importance_calibration()
    suggest_merges()
    print("[memory_manager] 审计完成。")

# ── 7. 卡片状态查询（用于 card_manager 展示剩余天数） ──

def get_card_status() -> list:
    """
    查询所有 final 卡片的活跃状态和剩余天数。
    返回 dict 列表，包含：
      - id, title, category, importance
      - is_permanent: 是否永久豁免
      - days_remaining: 剩余活跃天数（永久豁免返回 -1，已过期返回 0）
      - enabled: 当前是否在上下文中
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("""
            SELECT id, title, category, importance, 
                   created_at, last_referenced_at, enabled_in_context
            FROM cards WHERE review_status='final'
            ORDER BY created_at DESC
        """)
        rows = c.fetchall()
        now = datetime.now()
        result = []
        for row in rows:
            card_id, title, category, importance, created_at, last_ref, enabled = row
            
            # 判定永久豁免
            is_permanent = (category in ('milestone', 'commitments', 'deep_talks') or importance >= 8)
            
            if is_permanent:
                days_remaining = -1  # 永久
            else:
                # 取两个日期中较晚的那个
                created = datetime.fromisoformat(created_at) if created_at else None
                last = datetime.fromisoformat(last_ref) if last_ref else None
                
                if created and last:
                    ref_point = max(created, last)
                elif created:
                    ref_point = created
                elif last:
                    ref_point = last
                else:
                    ref_point = now
                
                elapsed = (now - ref_point).days
                days_remaining = max(0, 30 - elapsed)
            
            result.append({
                "id": card_id,
                "title": title,
                "category": category,
                "importance": importance,
                "is_permanent": is_permanent,
                "days_remaining": days_remaining,
                "enabled": bool(enabled)
            })
        return result
    except Exception as e:
        print(f"[memory_manager] 状态查询失败: {e}")
        return []
    finally:
        conn.close()

# ── 8. 删除卡片 ──

def delete_card(card_id: str) -> bool:
    """
    从数据库和 FAISS 索引中彻底删除一张卡片。
    返回 True 表示成功，False 表示失败。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        # 先从数据库删除
        c.execute("DELETE FROM cards WHERE id = ?", (card_id,))
        if c.rowcount == 0:
            print(f"[memory_manager] 删除失败：数据库中不存在 {card_id}")
            return False
        conn.commit()

        # 从 FAISS 索引移除
        try:
            from encoder import load_index, save_index
            index = load_index()
            index.remove_ids(np.array([int(card_id)], dtype=np.int64))
            save_index(index)
            print(f"[memory_manager] 已从索引中移除 {card_id}")
        except Exception as e:
            print(f"[memory_manager] 索引移除异常（数据库已删除）: {e}")

        print(f"[memory_manager] 卡片 {card_id} 已彻底删除")
        return True
    except Exception as e:
        print(f"[memory_manager] 删除失败: {e}")
        return False
    finally:
        conn.close()

if __name__ == "__main__":
    run_audit()