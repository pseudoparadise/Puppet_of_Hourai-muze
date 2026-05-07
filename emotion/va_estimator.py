"""
va_estimator.py - 情绪 VA 估测器
调 DeepSeek 对用户消息做效价-唤醒度结构化输出。
返回 {"valence": 1-10, "arousal": 1-10, "description": "简短情绪描述"}
"""
import json
import os
import requests

API_URL = "https://api.deepseek.com/v1/chat/completions"

def _load_api_key():
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    return config["global"]["deepseek_api_key"]

SYSTEM_PROMPT = """你是情绪分析助手。分析用户消息的情绪状态，输出 JSON：
{
  "valence": 1-10,    // 1=极度负面, 5=中性, 10=极度正面
  "arousal": 1-10,    // 1=极度平静, 5=中等, 10=极度激动
  "description": "简短中文情绪描述"
}
只输出 JSON，不要其他内容。"""

def estimate(text: str, max_retries: int = 2) -> dict:
    """
    估计用户消息的 VA 值。
    返回 {"valence": float, "arousal": float, "description": str}
    失败时返回中性默认值。
    """
    api_key = _load_api_key()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Opt-Out": "training"
    }
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text}
        ],
        "temperature": 0.2,
        "max_tokens": 100
    }

    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(API_URL, headers=headers, json=payload, timeout=15)
            if resp.status_code == 200:
                raw = resp.json()["choices"][0]["message"]["content"]
                result = json.loads(raw)
                # 校验字段
                v = float(result.get("valence", 5))
                a = float(result.get("arousal", 5))
                desc = result.get("description", "中性")
                return {
                    "valence": max(0.0, min(1.0, v / 10.0)),
                    "arousal": max(0.0, min(1.0, a / 10.0)),
                    "suggested_temperature": "hot" if a > 7 else ("warm" if a > 3 else "cool"),
                    "description": desc
                }
            else:
                if attempt < max_retries:
                    continue
        except Exception:
            if attempt < max_retries:
                continue

    return {"valence": 0.5, "arousal": 0.5, "suggested_temperature": "warm", "description": "中性（默认）"}

if __name__ == "__main__":
    test = estimate("我答应你每周陪我看一次星星")
    print("VA 估测结果:", test)