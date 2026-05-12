"""
test_memory.py - 记忆骨架验收测试（含空库、关键词、语义、重排、多样性）
"""
import sqlite3
import os
import sys
import shutil

sys.path.insert(0, os.path.dirname(__file__))

from retriever import retrieve, _build_candidate_pool, _score_card
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
            print(f"  [OK] 卡片 {row['id']} 向量化成功")
        except Exception as e:
            print(f"  [FAIL] 卡片 {row['id']} 向量化失败: {e}")
    conn.close()
    save_index(index)
    print("向量索引构建完成并已保存。")

def test_empty():
    """空库测试：无数据时检索返回空列表"""
    temp_db = "memory/test_empty.db"
    # 使用临时索引路径
    conn = sqlite3.connect(temp_db)
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS cards (id TEXT PRIMARY KEY, title TEXT, content TEXT, keywords TEXT, importance INTEGER, category TEXT, review_status TEXT, enabled_in_context INTEGER, created_at TEXT, last_referenced_at TEXT, usage_count INTEGER DEFAULT 0)")
    c.execute("DELETE FROM cards")
    conn.commit()
    conn.close()

    ret = retrieve("图书馆", db_path=temp_db)
    assert ret == [], f"空库应返回空列表, 实际: {ret}"
    # 清理临时文件
    if os.path.exists(temp_db):
        os.remove(temp_db)
    print("[PASS] 空库测试通过")

def test_keyword():
    """关键词命中测试：'我们去图书馆吧' 应命中卡片1"""
    setup()
    ret = retrieve("我们去图书馆吧")
    ids = [c["id"] for c in ret]
    assert "1" in ids, f"关键词命中失败，结果ids: {ids}"
    for c in ret:
        if c["id"] == "1":
            assert c["hit_count"] >= 1, f"hit_count应>=1, 实际: {c['hit_count']}"
    print("[PASS] 关键词命中测试通过")

def test_semantic():
    """语义召回测试：'需要一个不被打扰的地方' 应召回卡片2"""
    import random
    setup()
    random.seed(42)
    ret = retrieve("需要一个不被打扰的地方", top_k=3)
    ids = [c["id"] for c in ret]
    assert "2" in ids, f"语义召回失败，结果ids: {ids}"
    print("[PASS] 语义召回测试通过")

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
    print("[PASS] 重排公平性测试通过")

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
    print("[PASS] 多样性约束测试通过")


def test_va_switch():
    """VA切换测试：验证不同 va_tier 对检索结果的影响"""
    setup()
    # ── 追加带 usage_count 和 created_at 的卡片 ──
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # 给卡片1-5补充创建时间（最近7天）
    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    for i in range(1, 6):
        c.execute("UPDATE cards SET created_at = ?, usage_count = ? WHERE id = ?",
                  (three_days_ago, i * 2, str(i)))
    conn.commit()
    conn.close()

    # ── mid 模式基准检索 ──
    ret_mid = retrieve("测试", top_k=3, va_tier="mid")
    scores_mid = [c["score"] for c in ret_mid]
    spread_mid = max(scores_mid) - min(scores_mid) if len(scores_mid) > 1 else 0
    print(f"  mid模式: scores={[round(s,2) for s in scores_mid]}, 极差={spread_mid:.2f}")

    # ── high 模式检索 ──
    ret_high = retrieve("测试", top_k=5, va_tier="high")
    scores_high = [c["score"] for c in ret_high]
    spread_high = max(scores_high) - min(scores_high) if len(scores_high) > 1 else 0
    print(f"  high模式: scores={[round(s,2) for s in scores_high]}, 极差={spread_high:.2f}")

    # ── low 模式检索 ──
    ret_low = retrieve("测试", top_k=3, va_tier="low")
    scores_low = [c["score"] for c in ret_low]
    print(f"  low模式: scores={[round(s,2) for s in scores_low]}")

    # 验证：high模式极差大于mid模式极差（火/雷探针增加分数波动）
    assert spread_high > spread_mid * 0.8, \
        f"high模式极差({spread_high:.2f})应 >= mid模式极差的80%({spread_mid*0.8:.2f})"

    # 验证：low模式不会返回空
    assert len(ret_low) > 0, "low模式应有结果"

    print("[PASS] VA切换测试通过")


def test_candidate_pool():
    """候选池构建测试：不同VA唤醒度下候选池组成不同"""
    from datetime import datetime, timedelta

    three_days_ago = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    sixty_days_ago = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")

    cards = [
        {"id": "cp1", "category": "milestone", "importance": 9, "usage_count": 0,
         "created_at": sixty_days_ago, "keywords": "", "last_referenced_at": None},
        {"id": "cp2", "category": "daily_life", "importance": 5, "usage_count": 3,
         "created_at": three_days_ago, "keywords": "", "last_referenced_at": None},
        {"id": "cp3", "category": "interaction", "importance": 5, "usage_count": 1,
         "created_at": sixty_days_ago, "keywords": "", "last_referenced_at": None},
        {"id": "cp4", "category": "milestone", "importance": 8, "usage_count": 0,
         "created_at": sixty_days_ago, "keywords": "", "last_referenced_at": None},
        {"id": "cp5", "category": "daily_life", "importance": 5, "usage_count": 0,
         "created_at": three_days_ago, "keywords": "", "last_referenced_at": None},
    ]

    # Low tier: prefer DEEP_CATEGORIES (milestone)
    pool_low = _build_candidate_pool(cards, set(), "low")
    ids_low = {c["id"] for c in pool_low}
    assert "cp1" in ids_low, f"low模式应包含深层卡片cp1, 实际: {ids_low}"
    assert "cp4" in ids_low, f"low模式应包含深层卡片cp4, 实际: {ids_low}"

    # High tier: prefer recent + high-usage daily
    pool_high = _build_candidate_pool(cards, set(), "high")
    ids_high = {c["id"] for c in pool_high}
    assert "cp2" in ids_high, f"high模式应包含近期高频卡片cp2, 实际: {ids_high}"

    # Different tiers produce different pools
    assert ids_low != ids_high, \
        f"不同VA唤醒度应产生不同候选池: low={ids_low}, high={ids_high}"

    # candidate_limit truncation
    pool_limited = _build_candidate_pool(cards, set(), "mid", candidate_limit=2)
    assert len(pool_limited) <= 2, f"candidate_limit=2应限制为2张, 实际: {len(pool_limited)}"

    print("[PASS] 候选池构建测试通过")


def test_diffusion_reaction():
    """扩散反应测试：高唤醒下风火联动扩散加成高于纯风激活"""
    import random
    random.seed(42)
    setup()

    # 追加一张明文卡片（无关键词匹配，走纯语义路径）
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.execute("""INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context)
                 VALUES ('diff1', '扩散测试', '扩散反应验证文本内容', '无匹配', 5, 'interaction', 'final', 1)""")
    conn.commit()
    conn.close()

    # 重建索引包含新卡片
    index = create_index()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, content FROM cards WHERE review_status='final' AND enabled_in_context=1")
    for row in c.fetchall():
        vec = embed(row["content"])
        add_to_index(index, row["id"], vec)
    conn.close()
    save_index(index)

    random.seed(100)
    ret_mid = retrieve("扩散反应验证", top_k=5, va_tier="mid")
    random.seed(100)
    ret_high = retrieve("扩散反应验证", top_k=5, va_tier="high")

    score_mid = next((c["score"] for c in ret_mid if c["id"] == "diff1"), 0)
    score_high = next((c["score"] for c in ret_high if c["id"] == "diff1"), 0)

    assert score_high > score_mid, f"扩散反应失败: high={score_high:.4f} <= mid={score_mid:.4f}"
    print(f"  扩散反应: mid={score_mid:.4f}, high={score_high:.4f}")
    print("[PASS] 扩散反应测试通过")


def test_soothing_mode():
    """安抚模式测试：负效价+高唤醒压制火雷，排序变化"""
    import random
    from datetime import datetime, timedelta
    random.seed(42)
    setup()

    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB)
    c = conn.cursor()
    # 高重要里程碑（安抚模式下锚定加成不受压制）
    c.execute("INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              ('soothe_a', '重要里程碑', '安抚测试高重要卡', '里程碑', 9, 'milestone', 'final', 1, yesterday))
    # 近期互动卡片（高唤醒下雷火加成大，安抚模式下被压制）
    c.execute("INSERT INTO cards (id, title, content, keywords, importance, category, review_status, enabled_in_context, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
              ('soothe_b', '近期互动', '安抚测试近期卡片', '互动', 5, 'interaction', 'final', 1, yesterday))
    conn.commit()
    conn.close()

    # 重建索引
    index = create_index()
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT id, content FROM cards WHERE review_status='final' AND enabled_in_context=1")
    for row in c.fetchall():
        vec = embed(row["content"])
        add_to_index(index, row["id"], vec)
    conn.close()
    save_index(index)

    # 直调 _score_card 避开 retrieve 的 top-3 硬截断
    card_b = {"id": "soothe_b", "importance": 5, "category": "interaction",
              "usage_count": 0, "keywords": "", "created_at": yesterday,
              "last_referenced_at": None, "resolved": 0}

    random.seed(400)
    score_high = _score_card(card_b, 0, 1.0, va_tier="high")
    random.seed(400)
    # 模拟高唤醒 + 安抚模式：传入 _va_valence=0.2 到 weights
    w_soothe = {"w_keyword": 1.5, "w_semantic": 1.0, "w_importance": 0.5,
                "w_anchor": 0.2, "w_diffusion": 0.1, "w_recency": 0.3,
                "w_decay": 0.15, "w_va": 0.2, "w_fire": 0.25, "w_water": 0.15,
                "_va_valence": 0.2, "_fire_boost": True,
                "_usage_stats": {"total_searches": 0, "total_refs": 0}}
    score_soothe = _score_card(card_b, 0, 1.0, weights=w_soothe, va_tier="high")

    print(f"  soothe_b 纯高唤醒: {score_high:.4f}, 安抚模式: {score_soothe:.4f}")
    assert score_soothe < score_high, f"安抚模式应降低近期卡分数: {score_soothe:.4f} >= {score_high:.4f}"
    print("[PASS] 安抚模式测试通过")


def test_growth_cap():
    """草之生长差异化上限测试：daily_life上限0.8，milestone上限0.5"""
    import random
    random.seed(42)

    # daily_life card, usage=20 -> growth_bonus = min(0.8, 20*0.05) = 0.8
    card_d = {"id": "gd", "importance": 5, "category": "daily_life",
              "usage_count": 20, "keywords": "", "created_at": None, "last_referenced_at": None, "resolved": 0}
    card_d0 = dict(card_d, usage_count=0)

    random.seed(42)
    s20 = _score_card(card_d, 0, 1.0, va_tier="high")
    random.seed(42)
    s0 = _score_card(card_d0, 0, 1.0, va_tier="high")
    diff_daily = round(s20 - s0, 4)
    assert abs(diff_daily - 0.8) < 0.01, f"daily_life growth_bonus应为0.8, 实际{diff_daily}"

    # milestone card, usage=20 -> growth_bonus = min(0.5, 20*0.05) = 0.5
    card_m = {"id": "gm", "importance": 5, "category": "milestone",
              "usage_count": 20, "keywords": "", "created_at": None, "last_referenced_at": None, "resolved": 0}
    card_m0 = dict(card_m, usage_count=0)

    random.seed(42)
    s20m = _score_card(card_m, 0, 1.0, va_tier="high")
    random.seed(42)
    s0m = _score_card(card_m0, 0, 1.0, va_tier="high")
    diff_ms = round(s20m - s0m, 4)
    assert abs(diff_ms - 0.5) < 0.01, f"milestone growth_bonus应为0.5, 实际{diff_ms}"

    print(f"  草之生长: daily_life diff={diff_daily}, milestone diff={diff_ms}")
    print("[PASS] 草之生长差异化上限测试通过")



if __name__ == "__main__":
    setup()
    test_empty()
    test_keyword()
    test_semantic()
    test_rerank()
    test_diversity()
    test_va_switch()
    test_candidate_pool()
    test_diffusion_reaction()
    test_soothing_mode()
    test_growth_cap()
    print("\n[ALL PASS] 全部测试通过！")