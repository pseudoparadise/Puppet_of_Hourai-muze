"""
migrate_categories.py — 扩展 cards 表 category CHECK 约束，新增 4 个分类
运行一次即可：python memory/migrate_categories.py
"""
import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "cards.db")

def migrate():
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()

        # 检查是否已有新分类（尝试插入测试行）
        c.execute("PRAGMA table_info(cards)")
        cols = {row[1] for row in c.fetchall()}
        if "category" not in cols:
            print("[migrate] cards 表不存在，跳过迁移")
            return

        # SQLite 不支持 ALTER CHECK，用重建表方式迁移
        c.execute("""
            CREATE TABLE IF NOT EXISTS cards_new (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '',
                embedding BLOB,
                importance INTEGER NOT NULL DEFAULT 5 CHECK(importance BETWEEN 1 AND 10),
                category TEXT NOT NULL DEFAULT 'interaction'
                    CHECK(category IN (
                        'milestone','commitments','turning_points','deep_talks',
                        'interaction','preferences','real_world',
                        'daily_life','emotional','habits','erotic'
                    )),
                review_status TEXT NOT NULL DEFAULT 'pending'
                    CHECK(review_status IN ('pending','final')),
                enabled_in_context INTEGER NOT NULL DEFAULT 1,
                last_referenced_at TIMESTAMP,
                usage_count INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        c.execute("SELECT COUNT(*) FROM cards")
        old_count = c.fetchone()[0]

        c.execute("""
            INSERT INTO cards_new
            SELECT * FROM cards
        """)

        c.execute("DROP TABLE cards")
        c.execute("ALTER TABLE cards_new RENAME TO cards")

        c.execute("CREATE INDEX IF NOT EXISTS idx_cards_active ON cards(review_status, enabled_in_context)")

        conn.commit()
        print(f"[migrate] 迁移完成：{old_count} 条记录，新增 daily_life/emotional/habits/erotic 分类")
    except Exception as e:
        print(f"[migrate] 迁移失败: {e}")
        conn.rollback()
    finally:
        conn.close()

if __name__ == "__main__":
    migrate()
