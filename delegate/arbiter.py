"""
arbiter.py - 裁决者：纯逻辑判断引擎
在写卡/划卡之前判断用户意图是"完成"还是"新任务"。
不产生对话，不参与叙事，只输出 JSON 判断。
"""
import json
import os
import re
import sys
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompts", "arbiter.txt")


def _load_prompt():
    with open(PROMPT_PATH, "r", encoding="utf-8") as f:
        return f.read().strip()


def judge(user_input: str, pending_cards: list = None, recent_cards: list = None,
           diary_todos: list = None) -> dict:
    """
    裁决用户意图。
    diary_todos: 近3天日记的四象限待办摘要，用于拦截短期/长期待办。

    返回:
        {
            "judgment": "complete" | "new" | "update" | "ambiguous",
            "confidence": float,
            "target_card_ids": [str, ...],
            "reasoning": str,
            "suggested_action": "resolve" | "create" | "update" | "ask_for_clarification"
        }
    """
    pending_cards = pending_cards or []
    recent_cards = recent_cards or []
    diary_todos = diary_todos or []

    # ── 快路径：没有任何卡片可判断 → 必定是新任务 ──
    if not pending_cards and not recent_cards:
        return {
            "judgment": "new",
            "confidence": 1.0,
            "target_card_ids": [],
            "reasoning": "无pending/recent卡片",
            "suggested_action": "create"
        }

    # ── 快路径：硬关键词命中，直接判定 complete ──
    hard_complete_kw = [
        "拿回来了", "喝完了", "做好了", "拿到了", "搞定了", "做完了",
        "买好了", "干完了", "修好了", "打通了", "调通了", "收到了",
        "吃完了", "到手了", "好了好了", "搞完了", "办完了",
    ]
    user_has_complete_kw = any(kw in user_input for kw in hard_complete_kw)

    # ── 快路径：硬新任务关键词 ──
    hard_new_kw = ["明天买", "明天去", "明天要", "记得要", "需要做", "准备去", "打算买", "下周", "下个月"]

    # ── 调 DeepSeek 裁决 ──
    try:
        config_path = os.path.join(PROJECT_ROOT, "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)

        api_key = config["global"]["deepseek_api_key"]
        model = config["global"].get("model", "deepseek-v4-flash")

        # 精简 pending/recent 信息
        pending_summary = []
        for pc in pending_cards[:10]:
            pending_summary.append({
                "id": pc.get("id", ""),
                "title": pc.get("title", ""),
                "content": (pc.get("content", "") or "")[:100],
                "category": pc.get("category", ""),
                "keywords": pc.get("keywords", ""),
            })
        recent_summary = []
        for rc in recent_cards[:5]:
            recent_summary.append({
                "id": rc.get("id", ""),
                "title": rc.get("title", ""),
            })

        arbiter_prompt = _load_prompt()
        diary_str = "\n".join(diary_todos[:10]) if diary_todos else "无"
        context = f"""user_input: {user_input}
pending_cards: {json.dumps(pending_summary, ensure_ascii=False) if pending_summary else "[]"}
recent_cards: {json.dumps(recent_summary, ensure_ascii=False) if recent_summary else "[]"}
diary_todos (近3天四象限待办): {diary_str}"""

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": arbiter_prompt},
                {"role": "user", "content": context}
            ],
            "temperature": 0.1,  # 极低温度，逻辑判断不需要创造性
            "top_p": 0.5,
        }

        resp = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Opt-Out": "training"
            },
            json=payload,
            timeout=15
        )

        if resp.status_code == 200:
            raw = resp.json()["choices"][0]["message"]["content"]
            from shared import llm_to_json
            result = llm_to_json(raw)
            if result is None:
                raise ValueError(f"无法解析裁决者输出: {raw[:100]}")

            # 校验必要字段
            result.setdefault("judgment", "ambiguous")
            result.setdefault("confidence", 0.5)
            result.setdefault("target_card_ids", [])
            result.setdefault("reasoning", "裁决者返回不完整")
            result.setdefault("suggested_action", "ask_for_clarification")
            return result
        else:
            raise Exception(f"API status {resp.status_code}")

    except Exception as e:
        print(f"[裁决者] API 调用失败: {e}，降级为关键词判断")

    # ── 降级保护：关键词 + 特征重叠 ──
    return _fallback_judge(user_input, pending_cards, recent_cards,
                           user_has_complete_kw, hard_new_kw)


def _fallback_judge(user_input, pending_cards, recent_cards,
                    has_complete_kw, hard_new_kw):
    """降级裁决：纯本地关键词 + 特征重叠，不调 API。"""
    from shared import zh_stop_chars as _get_stop, zh_extract_features
    _STOP = _get_stop()

    user_feats = zh_extract_features(user_input)

    # ── 过期检测：用户说"还没/忘了/拖延"，匹配 pending/recent 卡，无完成信号 → overdue ──
    overdue_kw = ["还没", "忘了", "没洗", "没做", "拖延", "没拿", "还没做", "忘记"]
    if any(kw in user_input for kw in overdue_kw) and not has_complete_kw:
        all_cards = list(pending_cards) + list(recent_cards)
        best_id, best_score = None, 0
        for c in all_cards:
            ctext = (c.get("title", "") + " " + (c.get("content", "") or "")).lower()
            score = len(user_feats & zh_extract_features(ctext))
            if score > best_score:
                best_id, best_score = c.get("id", ""), score
        if best_id and best_score >= 2:
            return {
                "judgment": "overdue",
                "confidence": 0.85,
                "target_card_ids": [best_id],
                "reasoning": f"过期任务({best_score}重叠)，非完成",
                "suggested_action": "ask_for_new_time"
            }

    # ── 更新检测：高特征重叠但无完成信号 → update ──
    if not has_complete_kw:
        for pc in pending_cards:
            ptext = (pc.get("title", "") + " " + (pc.get("content", "") or "")).lower()
            score = len(user_feats & zh_extract_features(ptext))
            if score >= 4:  # 高重叠 → 同一事件，用户在补充信息
                return {
                    "judgment": "update",
                    "confidence": 0.75,
                    "target_card_ids": [pc.get("id", "")],
                    "reasoning": f"特征高重叠({score})，疑似更新而非新任务",
                    "suggested_action": "update"
                }

    if has_complete_kw:
        all_cards = list(pending_cards) + list(recent_cards)
        best_id, best_score = None, 0
        for c in all_cards:
            cid = c.get("id", "")
            ctext = (c.get("title", "") + " " + (c.get("content", "") or "")).lower()
            score = len(user_feats & zh_extract_features(ctext))
            if score > best_score:
                best_id, best_score = cid, score
        if best_id and best_score >= 2:
            return {
                "judgment": "complete",
                "confidence": 0.8,
                "target_card_ids": [best_id],
                "reasoning": f"完成关键词+特征重叠({best_score})",
                "suggested_action": "resolve"
            }

    if any(kw in user_input for kw in hard_new_kw):
        return {
            "judgment": "new",
            "confidence": 0.85,
            "target_card_ids": [],
            "reasoning": "新任务关键词命中",
            "suggested_action": "create"
        }

    # 模糊：有 pending 卡但无明确信号
    if pending_cards:
        # 检查是否有轻微重叠
        for pc in pending_cards:
            ptext = (pc.get("title", "") + " " + (pc.get("content", "") or "")).lower()
            if len(user_feats & zh_extract_features(ptext)) >= 1:
                return {
                    "judgment": "ambiguous",
                    "confidence": 0.5,
                    "target_card_ids": [pc.get("id", "")],
                    "reasoning": "有轻微特征重叠，无法确定意图",
                    "suggested_action": "ask_for_clarification"
                }
        return {
            "judgment": "new",
            "confidence": 0.7,
            "target_card_ids": [],
            "reasoning": "pending卡无重叠，按新任务处理",
            "suggested_action": "create"
        }

    return {
        "judgment": "new",
        "confidence": 0.6,
        "target_card_ids": [],
        "reasoning": "降级默认: 无pending卡且无明确信号",
        "suggested_action": "create"
    }


if __name__ == "__main__":
    # 快速测试
    test_input = "快递拿回来了"
    test_pending = [{
        "id": "test_001",
        "title": "拿快递",
        "content": "今天晚上8点前拿快递，不然门禁关",
        "category": "commitments",
        "keywords": "快递,门禁"
    }]
    result = judge(test_input, test_pending)
    print(json.dumps(result, ensure_ascii=False, indent=2))
