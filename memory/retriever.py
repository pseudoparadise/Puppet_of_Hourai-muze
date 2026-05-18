"""
retriever.py - 双路径检索 + 重排 Top 3（修复版）
根据导演后期要求，统一使用 encoder 提供的 load_index 接口。

FIX #1: 语义分支中 hit_count 变量未定义 → 改用 card["hit_count"]
FIX #2: 多样性约束逻辑 bug（重复添加卡片）→ 重写合并循环
FIX #3: 集成 encoder 的 ID 映射系统
NEW: 提取 _score_card() 独立打分函数，为未来重排算法优化留收口
"""
import sqlite3
import os
from .encoder import embed, load_index, search_index, DIM
import numpy as np
import random
from datetime import datetime

# ── 毒点23修复：拆分为不可变配置 + 独立状态 ──
SCORING_CONFIG = {
    "w_keyword": 1.5,       # 关键词命中权重
    "w_semantic": 1.0,      # 语义相似度权重
    "w_importance": 0.5,    # 重要度权重
    "w_anchor": 0.2,        # 岩之定锚权重（importance>=8 时激活）
    "w_diffusion": 0.1,     # 风之扩散随机加成上限
    "w_recency": 0.3,       # 雷之突进时间加权系数
    "w_decay": 0.15,        # 冰之冻结衰减系数
    "w_va": 0.2,            # VA 情绪加成权重
    "w_fire": 0.25,         # 火元素爆发搜索权重（中/高唤醒时激活）
    "w_water": 0.15,        # 水元素平滑修正权重（低/中唤醒时激活）
    "diversity_enabled": True,  # 是否启用多样性约束
    "min_categories": 2,    # 最小跨类别数
}

# 独立的状态计数器，不再污染权重配置（毒点23修复）
USAGE_STATS = {
    "total_searches": 0,
    "total_refs": 0,
    "fire_refs": 0,
    "water_refs": 0,
    "recency_refs": 0,
}

# ── P1-4: 深度卡片分类列表（模块级常量，供 _score_card 和 _build_candidate_pool 共享） ──
DEEP_CATEGORIES = {'milestone', 'commitments', 'deep_talks', 'turning_points', 'real_world'}
# ── P1-3: 日活卡片分类列表 ──
DAILY_CATEGORIES = {'daily_life', 'interaction', 'emotional', 'preferences', 'habits'}

# ── 和弦情绪四组：和弦名 → group ──
CHORD_GROUP = {
    # bright: 明亮坚定、推进、冲击
    'C': 'bright', 'Cmaj7': 'bright', 'G': 'bright', 'E': 'bright',
    # warm: 温暖宽广、摇曳
    'F': 'warm', 'Fmaj7': 'warm',
    # melancholy: 黯然、思念、忧郁
    'Am': 'melancholy', 'Am7': 'melancholy', 'Em': 'melancholy',
    'Em7': 'melancholy', 'Dm': 'melancholy', 'Dm7': 'melancholy',
    # tense: 焦灼、张力、冲突
    'G7': 'tense', 'D7': 'tense', 'B7': 'tense', 'Bm7': 'tense',
}

def _parse_chord_str(chord_str: str) -> dict:
    """从和弦字符串解析 group / bpm / dynamic。进行取首个和弦。"""
    import re as _re
    if not chord_str:
        return {}
    parts = chord_str.rsplit('.', 2)
    if len(parts) < 3:
        return {}
    name_raw, bpm_part, dynamic = parts[0], parts[1], parts[2]
    # 进行：取首个和弦名
    first = _re.findall(r'[A-G][a-z0-9]*', name_raw)
    group = CHORD_GROUP.get(first[0], None) if first else None
    try:
        bpm = int(bpm_part.replace('bpm', ''))
    except ValueError:
        bpm = None
    # bpm 档位
    bpm_tier = 'slow' if bpm and bpm <= 60 else ('fast' if bpm and bpm >= 130 else ('mid' if bpm else None))
    # dynamic 档位
    dyn_tier = 'soft' if dynamic in ('pp', 'p', 'mp') else ('strong' if dynamic in ('mf', 'f', 'ff') else None)
    return {'group': group, 'bpm': bpm, 'bpm_tier': bpm_tier, 'dynamic': dynamic, 'dyn_tier': dyn_tier}

def _chord_similarity(card: dict, query_chord: dict) -> float:
    """和弦收割层：比较卡片和弦和查询和弦，返回附加分。
    探针负责广撒网，和弦负责精收割——同组/同档优先。

    query_chord: {'group','bpm_tier','dyn_tier'} 或空 dict
    """
    if not query_chord:
        return 0.0
    card_chord_str = card.get('chord', '') or ''
    card_ch = _parse_chord_str(card_chord_str)
    if not card_ch:
        return 0.0  # 老卡无和弦，不参与和弦排序

    bonus = 0.0
    # 同组：情绪基调一致 → 最大加权
    if query_chord.get('group') and card_ch.get('group') == query_chord['group']:
        bonus += 0.20
    # 同 BPM 档：节奏感一致
    if query_chord.get('bpm_tier') and card_ch.get('bpm_tier') == query_chord['bpm_tier']:
        bonus += 0.10
    # 同力度档：能量级一致
    if query_chord.get('dyn_tier') and card_ch.get('dyn_tier') == query_chord['dyn_tier']:
        bonus += 0.08
    return bonus

def get_va_tier(arousal: float) -> str:
    """VA 唤醒度三层分档：低(0~0.3)→冰水 / 中(0.3~0.7)→雷火岩风 / 高(0.7~1)→超载"""
    if arousal < 0.30:
        return "low"
    elif arousal < 0.70:
        return "mid"
    else:
        return "high"


# ── EL-2: 虫洞跳跃 — 候选池构建（根据VA情绪和锚定集圈定语义搜索范围） ──
def _build_candidate_pool(all_cards: list, anchor_ids: set, va_tier: str,
                          va_description: str = None, candidate_limit: int = 50) -> list:
    """
    构建语义搜索候选池，复杂度从 O(N) 降为 O(K)。
    优先级：锚定集 > VA分档筛选 > 水之共鸣描述匹配
    候选池为空时退化为全表。
    """
    pool = []
    seen_ids = set()

    # 优先级 1：锚定集卡片
    for card in all_cards:
        if card['id'] in anchor_ids:
            pool.append(card)
            seen_ids.add(card['id'])

    # 优先级 2：VA 分档筛选
    now = datetime.now()
    for card in all_cards:
        if card['id'] in seen_ids:
            continue
        include = False
        if va_tier == 'high':
            # 高唤醒：优先近7天新卡 + 日活高频卡（usage≥3）
            if card.get('created_at'):
                try:
                    created = datetime.fromisoformat(card['created_at'])
                    if (now - created).days <= 7:
                        include = True
                except Exception:
                    print(f"[_build_candidate_pool] 日期解析失败 card_id={card.get('id', '?')}, created_at={card.get('created_at', 'None')}")
                    pass
            if not include and card.get('category') in DAILY_CATEGORIES and card.get('usage_count', 0) >= 3:
                include = True
        elif va_tier == 'low':
            # 低唤醒：优先深层卡片
            if card.get('category') in DEEP_CATEGORIES:
                include = True
        else:
            # ── 毒点24修复：中唤醒不进行全表追加，改为最后统一随机采样 ──
            # 随机采样在 return 前处理，锚定卡片不受限制
            pass
        if include:
            pool.append(card)
            seen_ids.add(card['id'])

    # 优先级 3：水之共鸣 — VA 描述关键词匹配
    if va_description:
        desc_lower = va_description.lower()
        for card in all_cards:
            if card['id'] in seen_ids:
                continue
            kws = card.get('keywords', '')
            if kws and any(kw.strip().lower() in desc_lower for kw in kws.split(',')):
                pool.append(card)
                seen_ids.add(card['id'])

    # 空池退化：包含全表
    if not pool:
        pool = list(all_cards)

    # ── 毒点24修复：中唤醒模式随机采样，避免新卡因插入顺序被排除 ──
    if va_tier == 'mid' and len(pool) > candidate_limit:
        import random as _random
        anchors_in_pool = [c for c in pool if c['id'] in anchor_ids]
        non_anchors = [c for c in pool if c['id'] not in anchor_ids]
        sampled = anchors_in_pool + _random.sample(non_anchors, min(len(non_anchors), candidate_limit - len(anchors_in_pool)))
        return sampled[:candidate_limit]
    else:
        return pool[:candidate_limit]


def _score_card(card: dict, hit_count: int, distance: float, weights: dict = None, anchor_ids: set = None, va_tier: str = "mid") -> float:
    """
    ── NEW: 独立打分函数，为未来记忆卡片重排算法优化收口 ──
    集成岩/风/雷/冰四探针 + 锚定集合加成

    card: 卡片 dict，包含 importance, category 等字段
    hit_count: 关键词命中次数
    distance: FAISS L2 距离（越小越相似）
    weights: 权重 dict，默认使用 SCORING_CONFIG
    """
    # ── ST-2: resolved 卡片沉底（分數×0.05），不改 importance 真實值 ──
    resolved_penalty = 0.05 if card.get('resolved') == 1 else 1.0

    w = weights or SCORING_CONFIG
    dist_sigmoid = 2.0 / (1.0 + np.exp(distance)) if distance < 10 else 0.0
    keyword_score = hit_count * w["w_keyword"]
    semantic_score = dist_sigmoid * w["w_semantic"]
    importance_score = card.get("importance", 5) * w["w_importance"]

    # 岩之定锚：importance >= 8 的卡片获得额外锚定加成
    anchor_bonus = 0
    if card.get('importance', 5) >= 8:
        anchor_bonus = w['w_anchor'] * (card['importance'] / 10.0)

    # 冰之锚定：锚定集合中的卡片获得额外加成
    if anchor_ids and card.get('id') in anchor_ids:
        anchor_bonus += w["w_anchor"] * 1.5

    # ── P1-4: 低唤醒深度卡锚定加成 ──
    if w.get('_deep_boost') and card.get('category') in DEEP_CATEGORIES:
        anchor_bonus += w.get('w_anchor', 0.2) * 0.5

    # 风之扩散：随机探索加成（模拟风元素的大范围探索）
    diffusion_bonus = 0
    if w.get('w_diffusion', 0) > 0:
        diffusion_bonus = random.uniform(0, w['w_diffusion'])

    # 雷之突进：近7天创建的卡片获得时间加权
    recent_bonus = 0
    if w.get('w_recency', 0) > 0 and card.get('created_at'):
        try:
            created = datetime.fromisoformat(card['created_at'])
            days_ago = (datetime.now() - created).days
            if days_ago <= 7:
                recent_bonus = w['w_recency'] * (1 - days_ago / 7)  # 越新加成越高
        except:
            pass

    # 冰之冻结：长期未引用卡片平滑降权
    decay_penalty = 0
    if w.get('w_decay', 0) > 0 and card.get('last_referenced_at'):
        try:
            last_ref = datetime.fromisoformat(card['last_referenced_at'])
            days_unused = (datetime.now() - last_ref).days
            if days_unused > 30:
                decay_penalty = w['w_decay'] * min(1.0, (days_unused - 30) / 60)  # 30天后开始衰减，90天达到最大
        except:
            pass

    # 火之爆发：中/高唤醒时随机扰动，重要卡片扰动更大
    fire_burst = 0
    if va_tier in ("high", "mid") and w.get('w_fire', 0) > 0:
        fire_burst = random.uniform(0, w['w_fire']) * (1 + card.get('importance', 5) / 10.0)
    # ── P2-2: 高唤醒火探针额外放大 ──
    if w.get('_fire_boost'):
        fire_burst *= 1.3

    # 水之平滑：低/中唤醒时小幅高斯修正，向重要性靠拢
    water_smooth = 0
    if va_tier in ("low", "mid") and w.get('w_water', 0) > 0:
        water_smooth = w['w_water'] * random.gauss(0, 0.3)

    # ── 负效价高唤醒安抚模式：情绪尖峰时压火雷、升水冰 ──
    # va_valence<0.3 对应极度负面情绪（0-1标尺），阈值基于估计器输出的经验值，可调。
    # 未来可加入滞回（hysteresis）来避免频繁切换。
    va_valence = w.get('_va_valence')
    if va_tier == "high" and va_valence is not None and va_valence < 0.3:
        fire_burst *= 0.3          # 火之爆发降为安抚温度
        recent_bonus *= 0.4        # 雷之突进衰减（情绪洪流时近期卡片非优先）
        water_smooth = w.get('w_water', 0.15) * random.gauss(0, 0.2)  # 水面浮起
    # ── GOA 元素反应（探针联动加分） ──
    # 蒸发反应：水+关键词命中 → 语义扩散
    if hit_count > 0 and va_tier in ("low", "mid") and w.get('w_water', 0) > 0:
        semantic_score += w['w_water'] * 0.5

    # 超载反应：火+雷同时激活 → recent_bonus 附加爆炸
    if va_tier == "high" and w.get('w_fire', 0) > 0 and w.get('w_recency', 0) > 0:
        recent_bonus += w['w_fire'] * 0.3

    # 冻结反应：冰+锚定命中 → anchor_bonus 加成
    if va_tier == "low" and anchor_ids and card.get('id') in anchor_ids:
        anchor_bonus += w.get('w_anchor', 0.2) * 0.3

    # ── FINAL-1: 扩散反应（风+火）：风扩散被火爆发放大 ──
    if diffusion_bonus > 0 and va_tier in ("high", "mid") and w.get('w_fire', 0) > 0:
        diffusion_bonus = diffusion_bonus * (1.0 + w['w_fire'] * 0.3)

    # ── VA 情绪描述匹配（水之共鸣） ──
    va_description = w.get('_va_description', '')
    if va_description and va_tier != "mid":
        card_kws = card.get('keywords', '').lower()
        desc_lower = va_description.lower()
        if any(kw.strip() in desc_lower for kw in card_kws.split(',')):
            water_smooth += w.get('w_va', 0.2) * 0.5

    # ── P1-3: 草之生长按category差异化上限 ──
    growth_bonus = 0
    usage = card.get('usage_count', 0)
    if usage > 0:
        growth_cap = 0.8 if card.get('category') in DAILY_CATEGORIES else 0.5
        growth_bonus = min(growth_cap, usage * 0.05)

    # ── P2-1: 绽放反应（草+水） ──
    if va_tier in ("low", "mid") and w.get('w_water', 0) > 0 and growth_bonus > 0:
        growth_bonus += w['w_water'] * 0.3

    # ── ST-3: VA 同符号微加权（卡片级情绪标签增强水之共鸣） ──
    card_valence = card.get('valence', 0)
    if va_valence is not None and card_valence != 0:
        if (va_valence > 0 and card_valence > 0) or (va_valence < 0 and card_valence < 0):
            water_smooth += 0.02

    # ── 阶段4.2：和弦 BPM/动态加权 ──
    chord_bpm = w.get('_chord_bpm')
    chord_dynamic = w.get('_chord_dynamic')
    if chord_bpm is not None:
        if chord_bpm <= 60:
            water_smooth += w.get('w_water', 0.15) * 0.4
        elif chord_bpm >= 130:
            fire_burst += w.get('w_fire', 0.25) * 0.3
    if chord_dynamic is not None:
        if chord_dynamic in ('f', 'ff'):
            fire_burst += w.get('w_fire', 0.25) * 0.2
        elif chord_dynamic in ('pp', 'p'):
            water_smooth += w.get('w_water', 0.15) * 0.3

    # ── 和弦收割层：探针广撒网，和弦精收割 ──
    chord_harvest = 0.0
    query_chord = w.get('_query_chord')
    if query_chord:
        chord_harvest = _chord_similarity(card, query_chord)

    # ── P2-4: 圣遗物计数器 ──
    USAGE_STATS["total_searches"] = USAGE_STATS.get("total_searches", 0) + 1

    return (
        keyword_score + semantic_score + importance_score +
        anchor_bonus + diffusion_bonus + recent_bonus - decay_penalty +
        fire_burst + water_smooth + growth_bonus + chord_harvest
    ) * resolved_penalty


def retrieve(query: str, top_k: int = 3, weights: dict = None,
             va_tier: str = "mid", va_description: str = None, va_valence: float = None,
             chord_bpm: int = None, chord_dynamic: str = None, chord_name: str = None) -> list:
    db_path = os.path.join(os.path.dirname(__file__), "cards.db")

    # ── VA 唤醒度三层分档：调整检索策略 ──
    w = weights or SCORING_CONFIG
    # ── 毒点32修复：deepcopy 避免跨调用污染原始配置 ──
    import copy
    effective_weights = copy.deepcopy(w)
    effective_k = top_k
    diversity_enabled = w.get("diversity_enabled", True)
    semantic_k_mult = 3  # search_index k 倍数

    if va_description:
        effective_weights['_va_description'] = va_description
    if va_valence is not None:
        effective_weights['_va_valence'] = va_valence
    if chord_bpm is not None:
        effective_weights['_chord_bpm'] = chord_bpm
    if chord_dynamic is not None:
        effective_weights['_chord_dynamic'] = chord_dynamic
    # 构建查询和弦 dict，供 _chord_similarity 收割用
    if chord_bpm is not None and chord_dynamic is not None:
        bpm_tier = 'slow' if chord_bpm <= 60 else ('fast' if chord_bpm >= 130 else 'mid')
        dyn_tier = 'soft' if chord_dynamic in ('pp', 'p', 'mp') else ('strong' if chord_dynamic in ('mf', 'f', 'ff') else None)
        # chord_name → group（进行取首个和弦）
        group = None
        if chord_name:
            import re as _re
            first = _re.findall(r'[A-G][a-z0-9]*', chord_name)
            if first:
                group = CHORD_GROUP.get(first[0])
        effective_weights['_query_chord'] = {
            'group': group,
            'bpm_tier': bpm_tier,
            'dyn_tier': dyn_tier,
        }

    if va_tier == "high":
        # 共鸣优先：搜更宽、语义权重 ↑、关键词权重 ↓、关闭多样性
        effective_k = max(5, top_k)
        effective_weights["w_semantic"] = w.get("w_semantic", 1.0) * 1.5
        effective_weights["w_keyword"] = w.get("w_keyword", 1.5) * 0.5
        diversity_enabled = False
        semantic_k_mult = 5
        # ── P2-2: 高唤醒雷/火探针放大 ──
        effective_weights['w_recency'] = w.get('w_recency', 0.3) * 1.5
        effective_weights['_fire_boost'] = True
    elif va_tier == "low":
        # 稳定陪伴：减少语义搜索范围、风扩散关闭、优先关键词
        effective_weights["w_diffusion"] = 0
        semantic_k_mult = 2
        # ── P1-4: 低唤醒深度卡锚定加成标记 ──
        effective_weights['_deep_boost'] = True

    # 加载锚定卡片集合
    anchor_ids = set()
    anchor_path = os.path.join(os.path.dirname(__file__), "anchor_set.json")
    if os.path.exists(anchor_path):
        try:
            import json
            with open(anchor_path, "r", encoding="utf-8") as f:
                anchor_data = json.load(f)
            anchor_ids = {c["id"] for c in anchor_data.get("cards", [])}
        except:
            pass

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    c.execute("SELECT id, keywords, importance, category, content, title, created_at, last_referenced_at, usage_count FROM cards WHERE review_status='final' AND enabled_in_context=1")
    all_cards = [dict(row) for row in c.fetchall()]

    query_lower = query.lower()
    keyword_hits = []
    for card in all_cards:
        kws = [kw.strip().lower() for kw in card.get("keywords", "").split(",") if kw.strip()]
        hit_count = sum(1 for kw in kws if kw in query_lower)
        if hit_count > 0:
            card["hit_count"] = hit_count
            card["distance"] = 1.0
            card["score"] = _score_card(card, hit_count, 1.0, effective_weights, anchor_ids, va_tier)
            keyword_hits.append(card)

    # ── EL-4: 虫洞跳跃 — 构建候选池，语义搜索仅限候选池内 ──
    candidate_pool = _build_candidate_pool(all_cards, anchor_ids, va_tier, va_description)
    candidate_ids = {c["id"] for c in candidate_pool}

    semantic_hits = []
    if len(keyword_hits) < top_k:
        try:
            query_vec = embed(query)
            index = load_index()
            if index.ntotal > 0:
                candidates = search_index(index, query_vec, k=min(10, max(effective_k * semantic_k_mult, 5)))
                keyword_ids = {c["id"] for c in keyword_hits}
                for cid, dist in candidates:
                    if cid in keyword_ids:
                        continue
                    # ── EL-4: 虫洞跳跃 — 仅处理候选池内卡片 ──
                    if cid not in candidate_ids:
                        continue
                    c.execute(
                        "SELECT id, keywords, importance, category, content, title, created_at, last_referenced_at, usage_count "
                        "FROM cards WHERE id=? AND review_status='final' AND enabled_in_context=1",
                        (cid,)
                    )
                    row = c.fetchone()
                    if row:
                        card = dict(row)
                        card["hit_count"] = 0  # ── FIX: 明确设0 ──
                        card["distance"] = dist
                        card["score"] = _score_card(card, 0, dist, effective_weights, anchor_ids, va_tier)  # ── FIX: hit_count=0 ──
                        semantic_hits.append(card)
        except Exception as e:
            print(f"[语义召回异常]: {e}")

    conn.close()

    seen = {c["id"]: c for c in keyword_hits}
    for c in semantic_hits:
        if c["id"] not in seen:
            seen[c["id"]] = c

    merged = sorted(seen.values(), key=lambda x: x["score"], reverse=True)

    result = []
    categories_used = set()

    for card in merged:
        if len(result) >= effective_k:
            break
        if (
            diversity_enabled
            and len(result) == effective_k - 1
            and len(categories_used) < w.get("min_categories", 2)
            and card["category"] in categories_used
        ):
            for later in merged[len(result):]:
                if later["category"] not in categories_used:
                    result.append(later)
                    categories_used.add(later["category"])
                    break
            if len(result) >= effective_k:
                break

        result.append(card)
        categories_used.add(card["category"])

    result = result[:effective_k]

    # ── P2-3: 高唤醒模式Top-3硬截断 ──
    if va_tier == "high":
        result = result[:3]

    output = []
    for card in result:
        output.append({
            "id": card["id"],
            "title": card["title"],
            "content": card["content"],
            "keywords": card["keywords"],
            "importance": card["importance"],
            "category": card["category"],
            "score": round(card["score"], 4),
            "hit_count": card.get("hit_count", 0),
            "distance": round(card.get("distance", 1.0), 4)
        })
    return output