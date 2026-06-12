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
import sys
import json
from encoder import embed, load_index, search_index, DIM
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import random
from datetime import datetime

# ── 毒点23修复：拆分为不可变配置 + 独立状态 ──
SCORING_CONFIG = {
    "w_keyword": 1.5,       # 关键词命中权重
    "w_semantic": 1.0,      # 语义相似度权重
    "w_importance": 0.5,    # 重要度权重
    "w_anchor": 0.08,       # 岩之定锚权重（importance>=8，已削至小数点后微调）
    "w_diffusion": 0.15,    # 风之扩散随机加成上限（微调↑ 0.1→0.15）
    "w_recency": 0.3,       # 雷之突进时间加权系数
    "w_decay": 0.2,         # 冰之冻结衰减系数（微调↑ 0.15→0.2）
    "w_va": 0.2,            # VA 情绪加成权重
    "w_fire": 0.25,         # 火元素爆发搜索权重（中/高唤醒时激活）
    "w_water": 0.2,         # 水元素平滑修正权重（微调↑ 0.15→0.2）
    "diversity_enabled": True,  # 是否启用多样性约束
    "min_categories": 2,    # 最小跨类别数
    # ── penalty（打破马太效应） ──
    "w_presence_penalty": 0.06,   # 本轮/上轮刚出现过的卡 → 扣分
    "w_repetition_penalty": 0.15, # 近3轮出现2+次 → 扣分
    "w_frequency_penalty": 0.03,  # 每个 usage_count 带来的负向拉力
    "frequency_penalty_cap": 0.20, # frequency penalty 上限
    # ── 传送锚点 ──
    "teleport_rate": 0.15,        # 霸榜卡传送概率
}

# 独立的状态计数器，不再污染权重配置（毒点23修复）
USAGE_STATS = {
    "total_searches": 0,
    "total_refs": 0,
    "fire_refs": 0,
    "water_refs": 0,
    "recency_refs": 0,
}

# ── 三轮检索追踪器：实现 presence / repetition penalty ──
_RECENT_ROUNDS = []  # [[card_id, ...], [card_id, ...], ...] 最近3轮
_MAX_ROUNDS = 3


def _track_retrieved(card_ids: list):
    """记录本轮召回的卡片 ID。"""
    _RECENT_ROUNDS.append(list(card_ids))
    if len(_RECENT_ROUNDS) > _MAX_ROUNDS:
        _RECENT_ROUNDS.pop(0)


def _compute_penalty(card_id: str, weights: dict) -> float:
    """
    计算三个温和 penalty 的总和（presence + repetition + frequency）。
    全部在 0.01~0.10 级别，只在分数接近时打破僵局。
    """
    penalty = 0.0
    w_presence = weights.get('w_presence_penalty', 0)
    w_repetition = weights.get('w_repetition_penalty', 0)
    w_frequency = weights.get('w_frequency_penalty', 0)
    freq_cap = weights.get('frequency_penalty_cap', 0.10)

    # ── presence penalty：本轮或上一轮出现过 → 微弱扣分 ──
    recent_flat = set()
    for round_ids in _RECENT_ROUNDS[-2:]:  # 最近2轮
        recent_flat.update(round_ids)
    if card_id in recent_flat:
        penalty += w_presence

    # ── repetition penalty：近3轮出现2+次 → 中度扣分 ──
    appearance_count = sum(1 for round_ids in _RECENT_ROUNDS if card_id in round_ids)
    if appearance_count >= 2:
        penalty += w_repetition * min(appearance_count - 1, 2)  # 出现3次扣2倍

    return min(penalty, 0.15)  # 总 penalty 上限


def _track_referenced(card_ids: list):
    """追踪被 AI 实际引用的卡片（区别于被检索但未引用的）。"""
    USAGE_STATS["total_refs"] = USAGE_STATS.get("total_refs", 0) + len(card_ids)


def artifact_adapt():
    """
    圣遗物自适应：每 100 次检索检查引用率，自动微调探针权重。
    引用率低 → 探索不足 → 提升扩散+火。引用率高 → 收敛 → 恢复默认。
    """
    total = USAGE_STATS.get("total_searches", 0)
    if total == 0 or total % 100 != 0:
        return

    refs = USAGE_STATS.get("total_refs", 0)
    hit_rate = refs / max(total, 1)
    print(f"[圣遗物自适应] 检索{total}次, 引用率={hit_rate:.2%}")

    # 引用率 < 15%：探索不足，提升扩散+火
    if hit_rate < 0.15:
        SCORING_CONFIG["w_diffusion"] = min(0.25, SCORING_CONFIG.get("w_diffusion", 0.15) + 0.02)
        SCORING_CONFIG["w_fire"] = min(0.35, SCORING_CONFIG.get("w_fire", 0.25) + 0.02)
        print(f"[圣遗物自适应] 探索不足 → 扩散={SCORING_CONFIG['w_diffusion']:.2f} 火={SCORING_CONFIG['w_fire']:.2f}")
    # 引用率 > 40%：检索精准，恢复默认
    elif hit_rate > 0.40:
        SCORING_CONFIG["w_diffusion"] = max(0.10, SCORING_CONFIG.get("w_diffusion", 0.15) - 0.01)
        SCORING_CONFIG["w_fire"] = max(0.20, SCORING_CONFIG.get("w_fire", 0.25) - 0.01)
        print(f"[圣遗物自适应] 检索精准 → 扩散={SCORING_CONFIG['w_diffusion']:.2f} 火={SCORING_CONFIG['w_fire']:.2f}")
    else:
        print(f"[圣遗物自适应] 引用率正常，维持当前权重")

    # 重置计数器（保留趋势）
    USAGE_STATS["total_searches"] = 0
    USAGE_STATS["total_refs"] = 0


# ── P1-4: 深度卡片分类列表（模块级常量，供 _score_card 和 _build_candidate_pool 共享） ──
DEEP_CATEGORIES = {'milestone', 'commitments', 'deep_talks', 'turning_points', 'real_world'}
# ── P1-3: 日活卡片分类列表 ──
DAILY_CATEGORIES = {'daily_life', 'interaction', 'emotional', 'preferences', 'habits', 'todo'}

# ── 分类自适应关键词权重：口癖类靠精确文本匹配，深层类靠语义 ──
CATEGORY_KW_BOOST = {
    "interaction": 1.5,      # 口癖：关键词吃到饱（「恶俗啊」≠「俗恶啊」）
    "preferences": 1.2,      # 偏好：关键词重要（「燕麦拿铁」「少糖」）
    "habits": 1.2,
    "real_world": 1.2,
    "daily_life": 1.0,       # 日常：正常
    "todo": 1.0,
    "emotional": 1.0,
    "erotic": 0.7,           # 深层：关键词降权，语义主导
    "deep_talks": 0.6,
    "milestone": 0.6,
    "turning_points": 0.6,
    "commitments": 0.8,
}

# ── 和弦情绪四组：按音程结构自动归类 ──
def _classify_chord(chord_name: str) -> str:
    """根据和弦名推断情绪分组。"""
    import re as _re
    m = _re.match(r'^[A-G](?:#|b)?(.+)?$', chord_name)
    quality = (m.group(1) or '') if m else ''

    # 减和弦 / 半减七 → tense
    if any(x in quality for x in ('dim', '°', 'ø', 'm7b5')):
        return 'tense'
    # 增和弦 → tense（不稳定）
    if any(x in quality for x in ('aug', '+')):
        return 'tense'
    # 变化属和弦 → tense
    if any(x in quality for x in ('7b', '7#', 'alt')):
        return 'tense'
    # 大七 → warm（柔化的大调和弦）
    if any(x in quality for x in ('maj', 'M7', 'Δ')):
        return 'warm'
    # 小调和弦家族 → melancholy
    if quality.startswith('m'):
        return 'melancholy'
    # 加音和弦 → warm（check 在属七正则之前，add9 ≠ 属九）
    if 'add' in quality or '6' in quality:
        return 'warm'
    # 属七/属九/属十一/属十三 → tense
    if _re.search(r'[79]|11|13', quality):
        return 'tense'
    # 挂留和弦 → bright
    if 'sus' in quality:
        return 'bright'
    # 大三和弦 / 强力和弦 → bright
    return 'bright'

def _parse_chord_str(chord_str: str) -> dict:
    """从和弦字符串解析 group / bpm / dynamic。进行取首个和弦。"""
    import re as _re
    if not chord_str:
        return {}
    parts = chord_str.rsplit('.', 2)
    if len(parts) < 3:
        return {}
    name_raw, bpm_part, dynamic = parts[0], parts[1], parts[2]
    first = _re.findall(r'[A-G][a-z0-9]*', name_raw)
    group = _classify_chord(first[0]) if first else None
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
def _safe_parse_ts(val):
    """薄封装 delegate_tools.parse_time，兼容 datetime 对象和 None。"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    try:
        from delegate_tools import parse_time
        return parse_time(str(val))
    except Exception:
        return None


def _build_candidate_pool(all_cards: list, anchor_ids: set, va_tier: str,
                          va_description: str | None = None, va_valence: float | None = None,
                          va_arousal: float | None = None, candidate_limit: int = 50) -> list:
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
            # 高唤醒：近7天新卡 + 日活高频卡 + 深层卡始终进入
            if card.get('category') in DEEP_CATEGORIES:
                include = True
            if not include and card.get('created_at'):
                try:
                    created = _safe_parse_ts(card['created_at'])
                    if created is not None and (now - created).days <= 7:
                        include = True
                except Exception:
                    pass
            if not include and card.get('category') in DAILY_CATEGORIES and card.get('usage_count', 0) >= 3:
                include = True
        elif va_tier == 'low':
            # 低唤醒：优先深层卡片
            if card.get('category') in DEEP_CATEGORIES:
                include = True
        else:
            # ── 中唤醒：开心时日常卡无门槛入池，伤心/中性时保持严格过滤 ──
            if card.get('category') in DEEP_CATEGORIES:
                include = True
            elif card.get('category') in DAILY_CATEGORIES:
                if va_valence is not None and va_valence < 0:
                    # 伤心/中性：严格过滤，只放有使用记录的
                    if card.get('usage_count', 0) >= 1:
                        include = True
                else:
                    # 开心 或 VA不可用：日常卡无门槛入池
                    include = True
            elif card.get('importance', 5) >= 6:
                include = True
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

    # ── 毒点24修复：截断时按 VA 坐标距离排序，离当前情绪最近的卡优先进池 ──
    if len(pool) > candidate_limit:
        import math as _math
        anchors_in_pool = [c for c in pool if c['id'] in anchor_ids]
        non_anchors = [c for c in pool if c['id'] not in anchor_ids]
        if va_valence is not None and va_arousal is not None:
            qv, qa = va_valence, va_arousal
            non_anchors.sort(key=lambda c: _math.sqrt(
                (qv - (c.get('valence') or 0)) ** 2 + (qa - (c.get('arousal') or 0.5)) ** 2
            ))
        non_anchors = non_anchors[:candidate_limit - len(anchors_in_pool)]
        return (anchors_in_pool + non_anchors)[:candidate_limit]
    return pool


def _score_card(card: dict, hit_count: float, distance: float, weights: dict | None = None, anchor_ids: set | None = None, va_tier: str = "mid", **kwargs) -> float:
    """
    ── NEW: 独立打分函数，为未来记忆卡片重排算法优化收口 ──
    集成岩/风/雷/冰四探针 + 锚定集合加成

    card: 卡片 dict，包含 importance, category 等字段
    hit_count: 关键词命中次数
    distance: FAISS L2 距离（越小越相似）
    weights: 权重 dict，默认使用 SCORING_CONFIG
    stamina_phase: 体力衰减相位 0.0(广撒网)→1.0(精挑)，默认 0.5
    """
    stamina_phase = kwargs.get('stamina_phase', 0.5)
    # ── ST-2: resolved 卡片沉底（分數×0.05），不改 importance 真實值 ──
    resolved_penalty = 0.05 if card.get('resolved') == 1 else 1.0

    w = weights or SCORING_CONFIG
    dist_sigmoid = 2.0 / (1.0 + np.exp(distance)) if distance < 10 else 0.0
    keyword_score = hit_count * w["w_keyword"]
    # ── 分类自适应关键词权重：口癖类↑ 深层类↓ ──
    kw_boost = CATEGORY_KW_BOOST.get(card.get("category", ""), 1.0)
    keyword_score *= kw_boost
    # ── type 分层权重：moment > quote > event > fact > insight > reflection ──
    type_boost = {"moment": 1.3, "quote": 1.25, "event": 1.15, "fact": 1.0, "insight": 1.05, "reflection": 1.0}
    type_mult = type_boost.get(card.get("type", "fact"), 1.0)
    semantic_score = dist_sigmoid * w["w_semantic"]
    importance_score = card.get("importance", 5) * w["w_importance"]

    # 岩之定锚：importance >= 8 的卡片获得微弱锚定加成
    anchor_bonus = 0
    if card.get('importance', 5) >= 8:
        anchor_bonus = w.get('w_anchor', 0.08) * (card['importance'] / 10.0) * 0.3

    # 冰之锚定：锚定集合中的卡片获得额外加成
    if anchor_ids and card.get('id') in anchor_ids:
        anchor_bonus += w["w_anchor"] * 1.5

    # ── P1-4: 低唤醒深度卡锚定加成 ──
    if w.get('_deep_boost') and card.get('category') in DEEP_CATEGORIES:
        anchor_bonus += w.get('w_anchor', 0.2) * 0.5

    # ── P1-5: emotional 类语义加成（关键词稀疏，靠语义浮出水面） ──
    if card.get('category') == 'emotional':
        semantic_score *= 2.5

    # 风之扩散：随机探索加成（模拟风元素的大范围探索）
    diffusion_bonus = 0
    if w.get('w_diffusion', 0) > 0:
        diffusion_bonus = random.uniform(0, w['w_diffusion'])

    # 雷之突进：最近被引用/创建的卡片获得时间加权（优先 last_referenced_at）
    recent_bonus = 0
    if w.get('w_recency', 0) > 0:
        ref_date_str = card.get('last_referenced_at') or card.get('created_at')
        if ref_date_str:
            try:
                ref_date = _safe_parse_ts(ref_date_str)
                if ref_date is not None:
                    days_ago = (datetime.now() - ref_date).days
                    if days_ago <= 7:
                        recent_bonus = w['w_recency'] * (1 - days_ago / 7)
                    elif days_ago <= 14:
                        recent_bonus = w['w_recency'] * 0.3  # 第二周残值
            except:
                pass

    # 冰之冻结：14天未引用开始衰减，与卡片重要性成正比（高重要性卡更不耐冷落）
    decay_penalty = 0
    if w.get('w_decay', 0) > 0 and card.get('last_referenced_at'):
        try:
            last_ref = datetime.fromisoformat(card['last_referenced_at'])
            days_unused = (datetime.now() - last_ref).days
            if days_unused > 14:
                imp = card.get('importance', 5)
                decay_penalty = w['w_decay'] * min(1.0, (days_unused - 14) / 45) * (imp / 5.0)
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

    # ── P1-3: 草之生长 — 饱和曲线，VA 分类差异化 ──
    # ☀️ 正向卡(val≥0.3, a≥0.3)：引用升温放大，多露脸
    # 🌫️ 混合态：保持原值，presence/repetition penalty 制衡不动的马太效应
    # 🌙 深层卡(val<0.1 或 <0.2且a<0.3)：平时节制，当前 VA 同为负时关键召回
    growth_bonus = 0
    usage = card.get('usage_count', 0)
    if usage > 0:
        growth_cap = 0.4 if card.get('category') in DAILY_CATEGORIES else 0.3
        effective_usage = min(usage, 10)
        growth_bonus = growth_cap * (1 - 1.0 / (1 + effective_usage * 0.3))

        card_v = card.get('valence', 0)
        card_a = card.get('arousal', 0.5)
        if card_v >= 0.3 and card_a >= 0.3:
            growth_bonus *= 1.5
        elif card_v < 0.1 or (card_v < 0.2 and card_a < 0.3):
            growth_bonus *= 0.5
            if va_valence is not None and va_valence < 0.1:
                growth_bonus += 0.10

    # ── P2-1: 绽放反应（草+水） ──
    if va_tier in ("low", "mid") and w.get('w_water', 0) > 0 and growth_bonus > 0:
        growth_bonus += w['w_water'] * 0.3

    # ── ST-3: VA 坐标距离加权（当前情绪 vs 卡片情绪签名） ──
    card_valence = card.get('valence', 0)
    card_arousal = card.get('arousal', 0.5)
    if va_valence is not None and (card_valence != 0 or card_arousal != 0.5):
        import math as _math
        cur_v = va_valence
        cur_a = w.get('_va_arousal', 0.5)  # 当前唤醒度
        # 坐标距离
        va_dist = _math.sqrt((cur_v - card_valence)**2 + (cur_a - card_arousal)**2)
        # 距离越近加分越多，最大 0.12，区分度足够
        va_coord_bonus = max(0, 0.12 - va_dist * 0.08)
        water_smooth += va_coord_bonus

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

    # ── 体力衰减：早期扩散↑锚定↓(广撒网)，后期扩散↓锚定↑(精挑) ──
    explore_factor = 1.0 - stamina_phase  # 1.0→0.0
    converge_factor = stamina_phase       # 0.0→1.0
    diffusion_bonus *= (0.7 + 0.6 * explore_factor)  # 早期 ×1.3, 后期 ×0.7
    anchor_bonus *= (0.5 + 1.0 * converge_factor)    # 早期 ×0.5, 后期 ×1.5
    fire_burst *= (0.6 + 0.8 * explore_factor)       # 早期 ×1.4, 后期 ×0.6

    # ── 宝箱奖励：3% 概率大跳 (0.15~0.30) ──
    treasure_bonus = 0.0
    if random.random() < 0.03:
        treasure_bonus = random.uniform(0.15, 0.30)
        USAGE_STATS["treasure_hits"] = USAGE_STATS.get("treasure_hits", 0) + 1

    # ── 三个温和 penalty：presence + repetition + frequency ──
    presence_repetition_penalty = _compute_penalty(card.get('id', ''), w)
    usage = card.get('usage_count', 0)
    freq_cap = w.get('frequency_penalty_cap', 0.10)
    frequency_penalty = min(freq_cap, usage * w.get('w_frequency_penalty', 0.01))

    # ── 副作用：写入探针分数供 trace 使用 ──
    card["_probes"] = {
        "keyword": round(keyword_score, 4),
        "semantic": round(semantic_score, 4),
        "importance": round(importance_score, 4),
        "anchor": round(anchor_bonus, 4),
        "diffusion": round(diffusion_bonus, 4),
        "recency": round(recent_bonus, 4),
        "decay": round(decay_penalty, 4),
        "fire": round(fire_burst, 4),
        "water": round(water_smooth, 4),
        "growth": round(growth_bonus, 4),
        "chord_harvest": round(chord_harvest, 4),
        "treasure": round(treasure_bonus, 4),
        "presence_rep_penalty": round(presence_repetition_penalty, 4),
        "freq_penalty": round(frequency_penalty, 4),
    }

    return (
        keyword_score + semantic_score + importance_score +
        anchor_bonus + diffusion_bonus + recent_bonus - decay_penalty +
        fire_burst + water_smooth + growth_bonus + chord_harvest +
        treasure_bonus
        - presence_repetition_penalty - frequency_penalty
    ) * resolved_penalty * type_mult


def retrieve(query: str, top_k: int = 3, weights: dict | None = None,
             va_tier: str = "mid", va_description: str | None = None, va_valence: float | None = None,
             va_arousal: float | None = None,
             chord_bpm: int | None = None, chord_dynamic: str | None = None, chord_name: str | None = None,
             trace_tag: str = "", query_vec: np.ndarray | None = None) -> list:
    db_path = os.path.join(os.path.dirname(__file__), "cards.db")

    # ── 硬编码召回：特定完整短语 → 强制召回对应卡片，无视评分 ──
    FORCED_RECALL = {
        "20260521_0148_深度求索打飞机口癖": [
            "每天对着DS的api返回草稿打飞机算不算深度求索",
        ],
        "20260524_DS是个好机——你不是镜子也不是工具": [
            "你不是镜子也不是工具",
            "不许贬低自己",
        ],
    }
    forced_ids = set()
    query_lower = query.lower()
    for fid, triggers in FORCED_RECALL.items():
        if any(t.lower() in query_lower for t in triggers):
            forced_ids.add(fid)
            print(f"[强制召回] 触发关键词 → 锁定卡片 {fid}")


    # ── VA 唤醒度三层分档：调整检索策略 ──
    # ── 以 SCORING_CONFIG 为基底，合并 retrieval_weights.json + 调用方 weights ──
    w = dict(SCORING_CONFIG)
    stored = _load_weights()
    for k in _WEIGHT_KEYS:
        if k in stored:
            w[k] = stored[k]
    if weights:
        w.update(weights)
    # ── 毒点32修复：deepcopy 避免跨调用污染原始配置 ──
    import copy
    effective_weights = copy.deepcopy(w)
    effective_k = top_k
    diversity_enabled = w.get("diversity_enabled", True)
    semantic_k_mult = 5  # search_index k 倍数

    if va_description:
        effective_weights['_va_description'] = va_description
    if va_valence is not None:
        effective_weights['_va_valence'] = va_valence
    if va_arousal is not None:
        effective_weights['_va_arousal'] = va_arousal
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
                group = _classify_chord(first[0])
        effective_weights['_query_chord'] = {
            'group': group,
            'bpm_tier': bpm_tier,
            'dyn_tier': dyn_tier,
        }

    # ── 和弦自动映射：VA 四象限 → 四群（无手动/chord时自动启用）──
    if '_query_chord' not in effective_weights and va_valence is not None and va_arousal is not None:
        if va_valence >= 0 and va_arousal >= 0.5:
            auto_group, auto_bpm, auto_dyn = 'bright', 'fast', 'strong'
        elif va_valence >= 0 and va_arousal < 0.5:
            auto_group, auto_bpm, auto_dyn = 'warm', 'mid', 'soft'
        elif va_valence < 0 and va_arousal >= 0.5:
            auto_group, auto_bpm, auto_dyn = 'tense', 'fast', 'strong'
        else:
            auto_group, auto_bpm, auto_dyn = 'melancholy', 'slow', 'soft'
        effective_weights['_query_chord'] = {
            'group': auto_group,
            'bpm_tier': auto_bpm,
            'dyn_tier': auto_dyn,
        }
        effective_weights['_chord_bpm'] = 140 if auto_bpm == 'fast' else (60 if auto_bpm == 'slow' else 90)
        effective_weights['_chord_dynamic'] = 'f' if auto_dyn == 'strong' else ('p' if auto_dyn == 'soft' else 'mf')

    # ── VA 阶段配置：velocity tracker 覆盖 VA 门控 ──
    phase_cfg = effective_weights.pop('_phase_cfg', None)

    # ── 混合态检测：embedding 极性判断替代 COGNITIVE_KW 关键词列表 ──
    # 复用 query_vec，避免重复 embed
    mixed_mode = False
    if va_tier == "high":
        try:
            from encoder import embed as _embed_mix
            _qv: np.ndarray | None = query_vec if query_vec is not None else getattr(retrieve, '_cached_query_vec', None)
            if _qv is None:
                _qv = _embed_mix(query)
                retrieve._cached_query_vec = _qv  # type: ignore
            _COG_REF: np.ndarray | None = getattr(retrieve, '_cog_ref_vec', None)
            _EMO_REF: np.ndarray | None = getattr(retrieve, '_emo_ref_vec', None)
            if _COG_REF is None:
                _COG_REF = _embed_mix("debug分析排查代码逻辑算法数据结构技术方案架构编译部署")
                _EMO_REF = _embed_mix("难过伤心哭泣崩溃绝望孤独害怕焦虑愤怒委屈想念")
                retrieve._cog_ref_vec = _COG_REF  # type: ignore
                retrieve._emo_ref_vec = _EMO_REF  # type: ignore
            import numpy as _np_mix
            assert _qv is not None
            assert _COG_REF is not None
            assert _EMO_REF is not None
            _dot_c = _np_mix.dot(_qv, _COG_REF)
            _dot_e = _np_mix.dot(_qv, _EMO_REF)
            _n_q = _np_mix.linalg.norm(_qv)
            _n_c = _np_mix.linalg.norm(_COG_REF)
            _n_e = _np_mix.linalg.norm(_EMO_REF)
            _cos_cog = float(_dot_c / (_n_q * _n_c)) if _n_q * _n_c > 0 else 0.0
            _cos_emo = float(_dot_e / (_n_q * _n_e)) if _n_q * _n_e > 0 else 0.0
            is_cognitive = _cos_cog > _cos_emo and _cos_cog > 0.25
            mixed_mode = is_cognitive
            if mixed_mode:
                print(f"[混合态] embedding极性: cog={_cos_cog:.3f} emo={_cos_emo:.3f} → 混合态")
            # 缓存 query_vec 供后续 FAISS 复用
            retrieve._cached_query_vec = _qv  # type: ignore
        except Exception:
            pass  # 降级：embedding 不可用时跳过混合态

    if phase_cfg:
        # VA 阶段配置覆盖（三阶段情绪弧）
        effective_weights['_fire_boost'] = phase_cfg.get('fire_boost', False)
        diversity_enabled = phase_cfg.get('diversity_enabled', True)
        effective_weights["w_water"] = w.get('w_water', 0.2) * phase_cfg.get('w_water_mult', 1.0)
        effective_weights['_deep_boost'] = phase_cfg.get('deep_boost', False)
        effective_weights["w_semantic"] = w.get("w_semantic", 1.0) * phase_cfg.get('semantic_mult', 1.0)
        if phase_cfg.get('fire_boost'):
            effective_weights['w_recency'] = w.get('w_recency', 0.3) * 1.3
        phase_name = {k: v for k, v in phase_cfg.items() if isinstance(v, bool) and v}
        print(f"[retriever] VA阶段覆盖: {list(phase_name.keys())}")
    elif va_tier == "high" and not mixed_mode:
        # 纯高唤醒：搜更宽、语义↑、关键词↓、多样性关闭、雷火放大
        effective_k = max(5, top_k)
        effective_weights["w_semantic"] = w.get("w_semantic", 1.0) * 1.5
        effective_weights["w_keyword"] = w.get("w_keyword", 1.5) * 0.5
        diversity_enabled = False
        semantic_k_mult = 5
        effective_weights['w_recency'] = w.get('w_recency', 0.3) * 1.5
        effective_weights['_fire_boost'] = True
    elif mixed_mode:
        # 混合态(一边哭一边递归): 火↑保持情感, 水↑分析平滑, 多样性ON, 语义温和
        effective_weights["w_semantic"] = w.get("w_semantic", 1.0) * 1.2
        effective_weights["w_keyword"] = w.get("w_keyword", 1.5) * 0.8
        effective_weights['w_recency'] = w.get('w_recency', 0.3) * 1.3
        effective_weights['_fire_boost'] = True
        effective_weights["w_water"] = w.get('w_water', 0.2) * 1.5
        diversity_enabled = True
        semantic_k_mult = 4
        print("[retriever] 混合态: 高唤醒+分析 → 火↑水↑ 多样性ON")
    elif va_tier == "low":
        # 低唤醒：风扩散关闭、关键词优先、深度卡加成
        effective_weights["w_diffusion"] = 0
        semantic_k_mult = 2
        effective_weights['_deep_boost'] = True

    # ── 语义主导缩放：扰动项缩到 3%，保留 importance 不砍（它是定海神针） ──
    for k in ("w_anchor", "w_recency", "w_diffusion", "w_decay"):
        effective_weights[k] = w.get(k, 0) * 0.03

    # 加载锚定卡片集合
    anchor_ids = set()
    anchor_path = os.path.join(os.path.dirname(__file__), "anchor_set.json")
    if os.path.exists(anchor_path):
        try:
            with open(anchor_path, "r", encoding="utf-8") as f:
                anchor_data = json.load(f)
            anchor_ids = {c["id"] for c in anchor_data.get("cards", [])}
        except:
            pass

    from shared import mode_retrieval_categories
    _mode_cats = mode_retrieval_categories()

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    if _mode_cats:
        _placeholders = ",".join("?" * len(_mode_cats))
        c.execute(f"SELECT id, keywords, importance, category, content, title, created_at, last_referenced_at, usage_count, valence, arousal, embedding FROM cards WHERE review_status='final' AND enabled_in_context=1 AND category IN ({_placeholders})", _mode_cats)
    else:
        c.execute("SELECT id, keywords, importance, category, content, title, created_at, last_referenced_at, usage_count, valence, arousal, embedding FROM cards WHERE review_status='final' AND enabled_in_context=1")
    all_cards = [dict(row) for row in c.fetchall()]

    def _kw_match_score(kw: str, ql: str) -> float:
        """单个关键词匹配得分：精确命中=1.0，Bigram重叠=0.7，无匹配=0.0。
        长句关键词（>10字）只做精确匹配，避免常见词片段误命中。"""
        if kw in ql:
            return 1.0
        if 3 <= len(kw) <= 10:
            # Bigram 重叠：拆成相邻二字片段，任一命中即软匹配
            bigrams = [kw[i:i+2] for i in range(len(kw) - 1)]
            if any(bg in ql for bg in bigrams):
                return 0.7
        return 0.0

    query_lower = query.lower()

    keyword_hits = []
    for card in all_cards:
        kws = [kw.strip().lower() for kw in card.get("keywords", "").split(",") if kw.strip()]
        hit_score = sum(_kw_match_score(kw, query_lower) for kw in kws)
        if hit_score > 0:
            card["hit_count"] = round(hit_score, 1)
            card["distance"] = 1.0
            card["score"] = _score_card(card, hit_score, 1.0, effective_weights, anchor_ids, va_tier)
            keyword_hits.append(card)
            # 口癖/梗卡：关键词命中 → 给 bonus，不看 ID 前缀
            _title = card.get('title', '')
            if '口癖' in _title or '梗' in _title:
                card['score'] += 3.0
                print(f"[口癖召回] 「{_title}」关键词命中 → +3.0 bonus (score={card['score']:.1f})")

    # ── EL-4: 虫洞跳跃 — 构建候选池，语义搜索仅限候选池内 ──
    candidate_pool = _build_candidate_pool(all_cards, anchor_ids, va_tier, va_description, va_valence, va_arousal)
    candidate_ids = {c["id"] for c in candidate_pool}

    semantic_hits = []
    if len(keyword_hits) < top_k:
        try:
            qv = query_vec if query_vec is not None else getattr(retrieve, '_cached_query_vec', None)
            if qv is None:
                qv = embed(query)
                retrieve._cached_query_vec = qv  # type: ignore
            index = load_index()
            if index.ntotal > 0:
                candidates = search_index(index, qv, k=max(effective_k * semantic_k_mult, 5))
                keyword_ids = {c["id"] for c in keyword_hits}
                for cid, dist in candidates:
                    if cid in keyword_ids:
                        continue
                    # ── EL-4: 虫洞跳跃 — 仅处理候选池内卡片 ──
                    if cid not in candidate_ids:
                        continue
                    c.execute(
                        "SELECT id, keywords, importance, category, content, title, created_at, last_referenced_at, usage_count, valence, arousal, embedding "
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

    # ── 硬编码召回：触发关键词 → 强制置顶对应卡片，无视评分 ──
    for fid in forced_ids:
        if fid in seen:
            seen[fid]["score"] = 99.0
            print(f"[强制召回] {seen[fid].get('title', fid)} 分数拉满置顶")
        else:
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                cc = conn.cursor()
                cc.execute(
                    "SELECT id, title, content, keywords, importance, category "
                    "FROM cards WHERE id=? AND review_status='final'",
                    (fid,)
                )
                row = cc.fetchone()
                conn.close()
                if row:
                    forced_card = dict(row)
                    forced_card["score"] = 99.0
                    forced_card["hit_count"] = 1
                    forced_card["distance"] = 0.0
                    seen[fid] = forced_card
                    print(f"[强制召回] {fid} 已置顶注入结果")
            except Exception:
                pass

    merged = sorted(seen.values(), key=lambda x: x["score"], reverse=True)
    pool_size = max(len(merged), 1)

    # ── 体力衰减：前段广撒网(扩散↑)，后段精挑(锚定↑) ──
    for i, card in enumerate(merged):
        stamina_phase = i / pool_size  # 0.0(队列前) → 1.0(队列尾)
        explore_factor = 1.0 - stamina_phase
        converge_factor = stamina_phase
        # 调制分数：前段扩散+火↑，后段锚定+水↑
        stamina_mod = (
            (0.7 + 0.6 * explore_factor) * card.get("score", 0) * 0.3  # 扩散分量
            + (0.5 + 1.0 * converge_factor) * card.get("score", 0) * 0.3  # 锚定分量
        )
        card["score"] = card["score"] + stamina_mod * 0.15  # 温和调制 15%

    # 重排
    merged.sort(key=lambda x: x["score"], reverse=True)

    # ── link 扩散：沿 link 边走一跳，邻居卡衰减权重加入候选池 ──
    LINK_DECAY = 0.60
    _expand_from = [c for c in merged[:5] if c.get("score", 0) > 0]
    _diffused_cards = []  # 记录扩散详情供 console 输出
    if _expand_from:
        try:
            from linker import get_linked_with_similarity as _get_linked, COMPOSITE_THRESHOLD as _LINK_MIN
            _neighbor_ids = set()
            _neighbor_sims = {}
            _neighbor_sources = {}  # nid → source card title
            for _card in _expand_from:
                for _nid, _nsim, *_ in _get_linked(_card["id"]):
                    if _nid not in seen:
                        if _nid not in _neighbor_ids or _nsim > _neighbor_sims.get(_nid, 0):
                            _neighbor_sims[_nid] = _nsim
                            _neighbor_sources[_nid] = _card.get("title", _card["id"])[:20]
                        _neighbor_ids.add(_nid)

            if _neighbor_ids:
                _conn_link = sqlite3.connect(db_path)
                _conn_link.row_factory = sqlite3.Row
                _clink = _conn_link.cursor()
                _placeholders = ",".join(["?" for _ in _neighbor_ids])
                _clink.execute(
                    f"SELECT id, keywords, importance, category, content, title, "
                    f"created_at, last_referenced_at, usage_count, valence, arousal, embedding "
                    f"FROM cards WHERE id IN ({_placeholders}) "
                    f"AND review_status='final' AND enabled_in_context=1",
                    list(_neighbor_ids)
                )
                # 拿 query vec（优先传入 > 缓存）做邻居相关性过滤
                _query_vec = query_vec if query_vec is not None else getattr(retrieve, '_cached_query_vec', None)
                _linked = 0
                for _row in _clink.fetchall():
                    _ncard = dict(_row)
                    _neblob = _ncard.pop("embedding", None)
                    # query-neighbor 余弦过滤：邻居必须在语义上和查询相关
                    if _query_vec is not None and _neblob is not None:
                        try:
                            _nvec = np.frombuffer(_neblob, dtype=np.float32)
                            _qdot = np.dot(_query_vec, _nvec)
                            _qnorm = np.linalg.norm(_query_vec) * np.linalg.norm(_nvec)
                            _qcos = float(_qdot / _qnorm) if _qnorm > 0 else 0.0
                            if _qcos < 0.40:
                                continue  # 语义不相关，跳过
                        except Exception:
                            pass
                    _ncard["hit_count"] = 0
                    _ncard["distance"] = 1.5
                    _base_score = _score_card(_ncard, 0, 1.5, effective_weights, anchor_ids, va_tier)
                    _nsim = _neighbor_sims.get(_ncard["id"], _LINK_MIN)
                    _ncard["score"] = _base_score * LINK_DECAY * (0.7 + 0.3 * _nsim)
                    _ncard["_link_diffused"] = True
                    merged.append(_ncard)
                    seen[_ncard["id"]] = _ncard
                    _linked += 1
                    _diffused_cards.append((
                        _ncard["title"][:25],
                        _neighbor_sources.get(_ncard["id"], "?"),
                        _nsim,
                        round(_ncard["score"], 3)
                    ))
                _conn_link.close()
                if _linked:
                    # 运维日志：写入 retrieval_traces 文件，不吐 stdout 泄漏卡片顺序
                    _verbose = trace_tag not in ("preflight", "bark")
                    if _verbose:
                        print(f"[link扩散] {_linked} 张邻居卡注入候选池 (decay={LINK_DECAY})")
                        for _dt, _ds, _dsim, _dscore in _diffused_cards:
                            print(f"  -> [{_dt}] <- {_ds}  (link_cos={_dsim:.3f} score={_dscore})")
                    # 始终写 trace log
                    try:
                        import uuid as _uuid_link
                        _trace_path = os.path.join(os.path.dirname(__file__), "retrieval_traces.jsonl")
                        _entry = {
                            "trace_id": str(_uuid_link.uuid4())[:8],
                            "tag": trace_tag, "linked": _linked,
                            "neighbors": [{"title": _dt, "source": _ds, "cos": round(_dsim, 4), "score": round(_dscore, 2)} for _dt, _ds, _dsim, _dscore in _diffused_cards]
                        }
                        with open(_trace_path, "a", encoding="utf-8") as _tf:
                            _tf.write(json.dumps(_entry, ensure_ascii=False) + "\n")
                    except Exception:
                        pass
        except Exception as _ld_e:
            print(f"[link扩散] 跳过: {_ld_e}")

    # 重排（含 link 扩散邻居）
    merged.sort(key=lambda x: x["score"], reverse=True)

    # ── Embedding 去重：card-card cos > 0.92 → 只留最高分 ──
    if diversity_enabled:
        _deduped = []
        _seen_embs = []
        for _card in merged:
            _emb = _card.get("embedding")
            if _emb is None:
                _deduped.append(_card)
                continue
            try:
                _vec = np.frombuffer(_emb, dtype=np.float32)
            except Exception:
                _deduped.append(_card)
                continue
            _dup = False
            for __, _sv in _seen_embs:
                _dot = np.dot(_vec, _sv)
                _norm = np.linalg.norm(_vec) * np.linalg.norm(_sv)
                if _norm > 0 and float(_dot / _norm) > 0.92:
                    _dup = True
                    break
            if _dup:
                continue
            _deduped.append(_card)
            _seen_embs.append((_card["id"], _vec))
        merged = _deduped

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

    # ── P2-3: 高唤醒模式Top-5截断（扩到5给VA匹配卡留空间） ──
    if va_tier == "high":
        result = result[:5]

    # ── 传送锚点：霸榜卡触发传送 ──
    if result and random.random() < w.get("teleport_rate", 0.15):
        # 检测霸榜：任意卡在近3轮都出现
        dominated_ids = set()
        for cid in set(c["id"] for c in result):
            appearances = sum(1 for round_ids in _RECENT_ROUNDS if cid in round_ids)
            if appearances >= min(len(_RECENT_ROUNDS), 3):
                dominated_ids.add(cid)
        if dominated_ids:
            # 从冰封层(decay>0)或未召回卡中随机传送
            frozen_pool = [c for c in merged if c["id"] not in {r["id"] for r in result}]
            if frozen_pool:
                teleport_card = random.choice(frozen_pool)
                # 替换分数最低的非锚定卡
                non_anchors = [(i, c) for i, c in enumerate(result) if c["id"] not in anchor_ids]
                if non_anchors:
                    worst_idx, _ = max(non_anchors, key=lambda x: x[1]["score"])
                    result[worst_idx] = teleport_card
                    print(f"[传送锚点] 霸榜卡{dominated_ids} → 传送「{teleport_card['title']}」")

    # ═══════════════════════════════════════════════════════════
    # 月笼协奏 — 月结晶迸发（混合态专属）
    # 触发条件: 混合态 active + 候选池 ≥3 张岩属性卡 (imp≥8)
    # 水底 = 低唤醒情绪基线 | 岩=高imp锚定卡
    # 迸发×3: 水卡加权 → 水岩共振 → 结晶传送
    # ═══════════════════════════════════════════════════════════
    _mixed_state = False
    try:
        _mva_path = os.path.join(PROJECT_ROOT, "manual_va.json")
        if os.path.exists(_mva_path):
            with open(_mva_path, "r", encoding="utf-8") as _mf:
                _mva = json.load(_mf)
            _mixed_state = _mva.get("mixed_state", False)
    except Exception:
        pass

    if _mixed_state:
        # 统计岩属性卡 (imp≥8，含 result 内 + merged 候选池)
        _rock_cards = [c for c in merged if c.get("importance", 5) >= 8]
        _rock_in_result = [c for c in result if c.get("importance", 5) >= 8]

        if len(_rock_in_result) >= 3:
            print(f"[月笼协奏] 混合态+{len(_rock_in_result)}岩 → 迸发月结晶")

            # Burst 1: 水卡加权 — 低唤醒情绪卡 (deep_talks/emotional) +0.15
            _water_ids = set()
            for c in merged:
                if c.get("category") in ("deep_talks", "emotional") and c.get("arousal", 0.5) <= 0.4:
                    c["score"] = c.get("score", 0) + 0.15
                    _water_ids.add(c["id"])
            _water_hit = len(_water_ids)
            print(f"[月结晶·壹] 水卡加权: {_water_hit} 张 +0.15")

            # Burst 2: 水岩共振 — 既是水底又是岩属性的卡 (emotional/deep_talks + imp≥8) +0.10
            _crystal_count = 0
            for c in merged:
                if c.get("category") in ("deep_talks", "emotional") and c.get("importance", 5) >= 8:
                    c["score"] = c.get("score", 0) + 0.10
                    _crystal_count += 1
            print(f"[月结晶·贰] 水岩共振: {_crystal_count} 张 +0.10")

            # Burst 3: 结晶传送 — 从 水卡 ∩ 岩卡 中随机一张未在 result 的注入
            _crystal_pool = [
                c for c in merged
                if c.get("category") in ("deep_talks", "emotional")
                and c.get("importance", 5) >= 8
                and c["id"] not in {r["id"] for r in result}
            ]
            if _crystal_pool:
                import random as _rand_cry
                _teleport = _rand_cry.choice(_crystal_pool)
                # 替换 result 中分数最低的非锚定卡
                _non_anchor_result = [
                    (i, c) for i, c in enumerate(result)
                    if c["id"] not in anchor_ids
                ]
                if _non_anchor_result:
                    _worst_idx, _ = max(_non_anchor_result, key=lambda x: x[1]["score"])
                    result[_worst_idx] = _teleport
                    print(f"[月结晶·叁] 结晶传送: 「{_teleport['title']}」注入结果")

            # 重排
            merged.sort(key=lambda x: x["score"], reverse=True)
            # 更新 result 排序
            result.sort(key=lambda x: x.get("score", 0), reverse=True)

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
            "distance": round(card.get("distance", 1.0), 4),
            "probes": card.get("_probes", {}),
        })
    # ── 追踪本轮召回，供下轮 presence/repetition penalty 使用 ──
    _track_retrieved([c["id"] for c in output])
    # ── 续命：更新 last_referenced_at + usage_count，防冰之冻结误伤 ──
    try:
        from memory.memory_manager import touch_cards
        touch_cards([c["id"] for c in output])
    except Exception:
        pass
    # ── 圣遗物自适应：每100次检索自动调参 ──
    artifact_adapt()
    # ── 写检索 trace 供 console.py 召回反馈面板读取 ──
    _write_retrieval_trace(query, va_tier, output, trace_tag)
    return output

def _write_retrieval_trace(query: str, va_tier: str, cards: list, tag: str = ""):
    """写入检索 trace 到 memory/retrieval_traces.jsonl，供 console.py 召回反馈面板使用。
    5 秒内相同查询跳过。"""
    import uuid as _uuid_trace
    from datetime import datetime as _dt_trace, timezone as _tz_trace, timedelta as _td_trace
    try:
        trace_path = os.path.join(os.path.dirname(__file__), "retrieval_traces.jsonl")

        now_dt = _dt_trace.now(_tz_trace(_td_trace(hours=8)))
        query_key = query[:60].strip()

        if os.path.exists(trace_path):
            with open(trace_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            if lines:
                try:
                    last = json.loads(lines[-1].strip())
                    last_ts = last.get("ts", "")
                    last_query = last.get("query", "")[:60].strip()
                    if last_query == query_key and last_ts:
                        last_dt = _dt_trace.strptime(last_ts, "%Y-%m-%d %H:%M:%S")
                        last_dt = last_dt.replace(tzinfo=_tz_trace(_td_trace(hours=8)))
                        if (now_dt - last_dt).total_seconds() < 5:
                            return
                except Exception:
                    pass

        entry = {
            "trace_id": str(_uuid_trace.uuid4())[:8],
            "ts": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "query": query[:200],
            "va_tier": va_tier,
            "cards": cards,
        }
        if tag:
            entry["tag"] = tag
        with open(trace_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        # 信息素双写：每张检索出的卡同时写入 mycelium
        try:
            from memory.mycelium import write as _mwrite
            for _c in cards:
                _mwrite("retrieval", _c["id"],
                        intensity=_c.get("score", 0.5),
                        halflife_s=3600,
                        meta=json.dumps({
                            "query": query[:80],
                            "va_tier": va_tier,
                            "tag": tag,
                        }, ensure_ascii=False))
        except Exception:
            pass  # mycelium 写入失败不阻塞主流程
    except Exception as e:
        import traceback as _tb_trace
        msg = f"[retrieval_trace 写入失败]\n{_tb_trace.format_exc()}"
        print(msg, file=sys.stderr)
        try:
            import tkinter.messagebox as _mb_trace
            _mb_trace.showerror("retriever trace 异常", f"写入 retrieval_traces.jsonl 失败:\n{e}")
        except Exception:
            pass


# ── 反馈系统：召回反馈面板使用的 API ──

_WEIGHTS_PATH = os.path.join(os.path.dirname(__file__), "retrieval_weights.json")
_TRACE_PATH = os.path.join(os.path.dirname(__file__), "retrieval_traces.jsonl")

_WEIGHT_KEYS = ["w_keyword", "w_semantic", "w_importance", "w_anchor",
                "w_diffusion", "w_recency", "w_decay", "w_va", "w_fire", "w_water",
                "w_presence_penalty", "w_repetition_penalty", "w_frequency_penalty",
                "frequency_penalty_cap", "teleport_rate"]


def _load_weights() -> dict:
    if os.path.exists(_WEIGHTS_PATH):
        try:
            with open(_WEIGHTS_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_weights(w: dict):
    from delegate_tools import atomic_write_json
    atomic_write_json(_WEIGHTS_PATH, w)


# ── 语义主导缩放下被 ×0.03 的扰动项 ──
_PERTURBATION_KEYS = {"w_anchor", "w_recency", "w_diffusion", "w_decay",
                      "w_presence_penalty", "w_repetition_penalty", "w_frequency_penalty",
                      "frequency_penalty_cap", "teleport_rate"}


def get_effective_weights() -> dict:
    """返回当前存储权重（SCORING_CONFIG + 人工反馈微调，不含 ×0.03 缩放）。"""
    stored = _load_weights()
    effective = dict(SCORING_CONFIG)
    for k in _WEIGHT_KEYS:
        if k in stored:
            effective[k] = stored[k]
        else:
            effective[k] = SCORING_CONFIG.get(k, 0)
    return {k: round(v, 3) for k, v in effective.items()}


def record_feedback(trace_id: str, card_id: str, is_good: bool):
    """在 retrieval_traces.jsonl 中标记某张卡的反馈。"""
    if not os.path.exists(_TRACE_PATH):
        return
    lines = []
    updated = False
    with open(_TRACE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("trace_id") == trace_id:
                    for c in entry.get("cards", []):
                        if c.get("id") == card_id:
                            c["feedback"] = "good" if is_good else "bad"
                            updated = True
                            break
                lines.append(json.dumps(entry, ensure_ascii=False))
            except Exception:
                lines.append(line.strip())
    if updated:
        with open(_TRACE_PATH, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print(f"[retriever] 反馈已记录: trace={trace_id} card={card_id} {'✓' if is_good else '✗'}")


def apply_feedback_adjustments() -> dict:
    """汇总所有反馈，微调探针权重。返回 {'good': N, 'bad': N}。"""
    if not os.path.exists(_TRACE_PATH):
        return {"good": 0, "bad": 0}

    good_cards = []
    bad_cards = []

    with open(_TRACE_PATH, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
            except Exception:
                continue
            for c in entry.get("cards", []):
                fb = c.get("feedback", "")
                probes = c.get("probes", {})
                if fb == "good":
                    good_cards.append(probes)
                elif fb == "bad":
                    bad_cards.append(probes)

    if not good_cards and not bad_cards:
        return {"good": 0, "bad": 0}

    # 探针键名 → 权重键名映射（探针使用短名，权重用 w_ 前缀）
    _PROBE_TO_WEIGHT = {
        "keyword": "w_keyword", "semantic": "w_semantic", "importance": "w_importance",
        "anchor": "w_anchor", "diffusion": "w_diffusion", "recency": "w_recency",
        "decay": "w_decay", "fire": "w_fire", "water": "w_water",
    }

    deltas = {k: 0.0 for k in _WEIGHT_KEYS}

    for probes in good_cards:
        sorted_probes = sorted(
            [(k, v) for k, v in probes.items() if k in _PROBE_TO_WEIGHT],
            key=lambda x: x[1], reverse=True
        )
        for k, v in sorted_probes[:3]:
            if v > 0:
                wk = _PROBE_TO_WEIGHT[k]
                step = 0.003 if wk in _PERTURBATION_KEYS else 0.015
                deltas[wk] += step

    for probes in bad_cards:
        sorted_probes = sorted(
            [(k, v) for k, v in probes.items() if k in _PROBE_TO_WEIGHT],
            key=lambda x: x[1], reverse=True
        )
        for k, v in sorted_probes[:3]:
            if v > 0:
                wk = _PROBE_TO_WEIGHT[k]
                step = 0.005 if wk in _PERTURBATION_KEYS else 0.02
                deltas[wk] -= step

    stored = _load_weights()
    for k in _WEIGHT_KEYS:
        base = stored.get(k, SCORING_CONFIG.get(k, 0))
        adjusted = base + deltas[k]
        lo = SCORING_CONFIG.get(k, 0) * 0.3
        hi = SCORING_CONFIG.get(k, 0) * 3.0
        stored[k] = round(max(lo, min(hi, adjusted)), 3)

    _save_weights(stored)
    print(f"[retriever] 权重微调完成: ✓{len(good_cards)}张 ✗{len(bad_cards)}张 → {get_effective_weights()}")
    return {"good": len(good_cards), "bad": len(bad_cards)}