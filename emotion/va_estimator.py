"""
va_estimator.py - 情绪 VA 估测器（修复版）

FIX: suggested_temperature 使用归一化后的 arousal (0-1) 而非原始值 (1-10)
"""
import json
import os
import requests

import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__))))

def _load_config():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        return json.load(f)["global"]

SYSTEM_PROMPT = """你是情绪分析助手。分析用户消息的情绪状态，输出 JSON：
{
  "valence": 1-10,
  "arousal": 1-10,
  "description": "简短中文情绪描述"
}
只输出 JSON，不要其他内容。"""

def estimate(text: str, max_retries: int = 2) -> dict:
    cfg = _load_config()
    api_key = cfg["deepseek_api_key"]
    api_url = cfg.get("api_url", "https://api.deepseek.com/v1/chat/completions")
    model = cfg.get("model", "deepseek-v4-flash")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Opt-Out": "training"
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ],
        "temperature": 0.2,
        "max_tokens": 100
    }

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(api_url, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                raw = resp.json()["choices"][0]["message"]["content"]
                # ── FIX: 处理截断JSON ──
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    import re
                    match = re.search(r'\{.*\}', raw, re.DOTALL)
                    if match:
                        result = json.loads(match.group())
                    else:
                        raise

                v_raw = float(result.get("valence", 5))
                a_raw = float(result.get("arousal", 5))
                desc = result.get("description", "中性")

                # 归一化到 0-1
                v = max(0.0, min(1.0, v_raw / 10.0))
                a = max(0.0, min(1.0, a_raw / 10.0))

                # ── FIX: 用归一化后的 a (0-1) 判断温度 ──
                if a > 0.7:
                    temp = "hot"
                elif a > 0.3:
                    temp = "warm"
                else:
                    temp = "cool"

                return {
                    "valence": v,
                    "arousal": a,
                    "suggested_temperature": temp,
                    "description": desc
                }
            else:
                if attempt < max_retries:
                    continue
        except Exception:
            if attempt < max_retries:
                continue

    return {"valence": 0.5, "arousal": 0.5, "suggested_temperature": "warm", "description": "中性（默认）"}


# ═══════════════════════════════════════════════════
# VA 速度追踪器：积累历史坐标，检测三阶段情绪弧
# ═══════════════════════════════════════════════════
_VA_HISTORY = []
_MAX_HISTORY = 5


def track_va(valence: float, arousal: float) -> dict:
    global _VA_HISTORY
    _VA_HISTORY.append((valence, arousal))
    if len(_VA_HISTORY) > _MAX_HISTORY:
        _VA_HISTORY.pop(0)

    if len(_VA_HISTORY) >= 2:
        prev_v, prev_a = _VA_HISTORY[-2]
        delta_v = valence - prev_v
        delta_a = arousal - prev_a
    else:
        delta_v, delta_a = 0, 0

    # 优先级: cognitive_surge > emotional_flood > recovery > normal
    # 因为 emotional_flood → cognitive_surge 的过渡需要优先检测上升趋势
    if arousal > 0.45 and delta_v > 0.08:
        phase = "cognitive_surge"   # 效价快速回升 → 理智涌入（即使仍高唤醒）
    elif arousal > 0.70 and valence < 0.35:
        phase = "emotional_flood"   # 高唤醒高负效价，无上升趋势 → 感性洪水
    elif arousal < 0.45:
        phase = "recovery"
    else:
        phase = "normal"

    return {
        "phase": phase, "valence": valence, "arousal": arousal,
        "delta_v": delta_v, "delta_a": delta_a, "history_len": len(_VA_HISTORY),
    }


def va_phase_config(phase: str) -> dict:
    if phase == "emotional_flood":
        return {"fire_boost": True, "diversity_enabled": False,
                "w_water_mult": 0.5, "deep_boost": True, "semantic_mult": 1.8}
    elif phase == "cognitive_surge":
        return {"fire_boost": True, "diversity_enabled": True,
                "w_water_mult": 1.5, "deep_boost": False, "semantic_mult": 1.2}
    elif phase == "recovery":
        return {"fire_boost": False, "diversity_enabled": True,
                "w_water_mult": 1.0, "deep_boost": False, "semantic_mult": 1.0}
    else:
        return {"fire_boost": False, "diversity_enabled": True,
                "w_water_mult": 1.0, "deep_boost": False, "semantic_mult": 1.0}

if __name__ == "__main__":
    test = estimate("我答应你每周陪我看一次星星")
    print("VA 估测结果:", test)
