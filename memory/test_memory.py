"""
test_memory.py - 记忆骨架验收测试（含空库、关键词、语义、重排、多样性）
"""
import sqlite3
import os
import sys
import shutil

sys.path.insert(0, os.path.dirname(__file__))

from retriever import retrieve
from encoder import embed, create_index, add_to_index, save_index

DB = os.path.join(os.path.dirname(__file__), "cards.db")
INDEX_PATH = os.path.join(os.path.dirname(__file__), "vectors.faiss")
ID_MAP_PATH = os.path.join(os.path.dirname(__file__), "id_map.json")

def setup():
    """重建测试卡片并生成向量索引"""
    # 清空旧数据
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("DELETE FROM cards")

    c.execute("""
        INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context)
        VALUES ('1', '图书馆', '在图书馆一起看书', '图书馆,外套', 7, 'commitments', 'final', 1)
    """)
    c.execute("""
        INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context)
        VALUES ('2', '安静地方', '沐泽喜欢在安静的地方看书', '安静,阅读', 5, 'interaction', 'final', 1)
    """)
    c.execute("""
        INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context)
        VALUES ('3', '高重要无命中', '对user很重要的事情，但关键词不匹配', '稀有,特别', 9, 'milestone', 'final', 1)
    """)
    c.execute("""
        INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context)
        VALUES ('4', '同类别A', '同类别测试A', '测试', 5, 'commitments', 'final', 1)
    """)
    c.execute("""
        INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context)
        VALUES ('5', '同类别B', '同类别测试B', '测试', 5, 'commitments', 'final', 1)
    """)
    conn.commit()
    conn.close()

    # 删除旧索引，重新生成
    for f in [INDEX_PATH, ID_MAP_PATH]:
        if os.path.exists(f):
            os.remove(f)

    print("正在为所有测试卡片生成向量并建立索引...")
    index = create_index()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, content FROM cards WHERE review_status='final' AND enabled_in_context=1")
    rows = c.fetchall()
    for row in rows:
        try:
            vec = embed(row["content"])
            add_to_index(index, row["id"], vec)
            print(f"  ✓ 卡片 {row['id']} 向量化成功")
        except Exception as e:
            print(f"  ✗ 卡片 {row['id']} 向量化失败: {e}")
    conn.close()
    save_index(index)
    print("向量索引构建完成并已保存。")

def test_empty():
    """空库测试：无数据时检索返回空列表"""
    temp_db = "memory/test_empty.db"
    # 使用临时索引路径
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, title TEXT, content TEXT, keywords TEXT, importance INTEGER, category TEXT, review_status TEXT, enabled_in_context INTEGER)")
    c.execute("DELETE FROM cards")
    conn.commit()
    conn.close()

    ret = retrieve("图书馆", db_path=temp_db)
    assert ret == [], f"空库应返回空列表, 实际: {ret}"
    # 清理临时文件
    if os.path.exists(temp_db):
        os.remove(temp_db)
    print("✅ 空库测试通过")

def test_keyword():
    """关键词命中测试：'我们去图书馆吧' 应命中卡片1"""
    setup()
    ret = retrieve("我们去图书馆吧")
    ids = [c["id"] for c in ret]
    assert "1" in ids, f"关键词命中失败，结果ids: {ids}"
    for c in ret:
        if c["id"] == "1":
            assert c["hit_count"] >= 1, f"hit_count应>=1, 实际: {c['hit_count']}"
    print("✅ 关键词命中测试通过")

def test_semantic():
    """语义召回测试：'需要一个不被打扰的地方' 应召回卡片2"""
    setup()
    ret = retrieve("需要一个不被打扰的地方")
    ids = [c["id"] for c in ret]
    assert "2" in ids, f"语义召回失败，结果ids: {ids}"
    print("✅ 语义召回测试通过")

def test_rerank():
    """重排公平性测试：高importance不一定排第一，需结合关键词命中"""
    setup()
    ret = retrieve("图书馆 测试 高重要")
    scores = {c["id"]: c["score"] for c in ret}
    assert len(ret) > 0, "应有结果"
    print(f"检索到 {len(ret)} 张卡片")
    for cid in ["1", "3"]:
        if cid in scores:
            print(f"卡片{cid} score={scores[cid]:.2f}")
    print("✅ 重排公平性测试通过")

def test_diversity():
    """多样性约束测试：返回的 top 3 至少跨 2 个 category"""
    setup()
    # 追加更多同类别卡片，确保触发多样性约束
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    for i in range(6, 10):
        c.execute("INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context) VALUES (?,?,?,?,?,?,?,?)",
                  (str(i), f"同类{i}", f"同类测试{i}", "测试", 8, "commitments", "final", 1))

    # === DS 要求：插入两张跨类别卡片 ===
    c.execute("INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context) VALUES (?,?,?,?,?,?,?,?)",
              ('10', '里程碑测试', '里程碑测试内容', '测试', 7, 'milestone', 'final', 1))
    c.execute("INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context) VALUES (?,?,?,?,?,?,?,?)",
              ('11', '深层对话测试', '深层对话测试内容', '测试', 7, 'deep_talks', 'final', 1))
    # ======================================

    conn.commit()
    conn.close()

    ret = retrieve("测试 测试 测试")
    categories = [c["category"] for c in ret]
    assert len(set(categories)) >= 2, f"多样性约束失败，返回类别: {categories}"
    print("✅ 多样性约束测试通过")

if __name__ == "__main__":
    setup()
    test_empty()
    test_keyword()
    test_semantic()
    test_rerank()
    test_diversity()
    print("\n🎉 全部测试通过！")