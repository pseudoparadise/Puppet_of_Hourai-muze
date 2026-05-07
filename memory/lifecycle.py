"""
lifecycle.py - 卡片生命周期管理
负责续命、引用更新、活跃状态管理。
"""
import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

def renew_card(card_id: str) -> bool:
    """
    续命：更新卡片的 last_referenced_at 并令 usage_count += 1。
    返回 True 表示成功，False 表示卡片不存在或更新失败。
    """
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        now_utc = datetime.now(timezone.utc)
        now = now_utc.isoformat()
        c.execute(
            "UPDATE cards SET last_referenced_at = ?, usage_count = usage_count + 1 WHERE id = ?",
            (now, card_id)
        )
        if c.rowcount == 0:
            return False
        conn.commit()
        return True
    except Exception as e:
        print(f"[lifecycle] 续命失败 card_id={card_id}: {e}")
        return False
    finally:
        conn.close()

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
        print(f"[lifecycle] 活跃状态已更新，当前时间: {now_utc.isoformat()}")
    except Exception as e:
        print(f"[lifecycle] 活跃状态更新失败: {e}")
    finally:
        conn.close()

if __name__ == "__main__":
    # 测试续命
    print("测试续命功能...")
    # 假定已有卡片 id='test1'
    renew_card("test1")
    update_active_status()