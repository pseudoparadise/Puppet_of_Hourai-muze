"""
delegate_tools.py - DeepSeek API 委托调用（修复版）

FIX: config.json 路径基于 __file__ 解析，不依赖 CWD
"""
import json
import os
import requests

# ── FIX: 确定项目根目录 ──
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_CONFIG_PATH = os.path.join(_PROJECT_ROOT, "config.json")

SYSTEM_PROMPT = """你是一个系统维护AI。你的任务是执行用户委托的具体操作。
规则：
1. 直接输出结果，不要寒暄，不要修饰。
2. 如果任务是"写日记"，输出一篇200字以内的日记。
3. 如果任务是"检索记忆"，输出你找到的相关记忆摘要。
4. 如果任务是其他操作，根据任务描述直接执行并输出结果。
不要添加任何额外解释。"""

def delegate(task_description, context=""):
    """调DeepSeek API执行委托任务"""
    # ── FIX: 使用绝对路径读取配置 ──
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        config = json.load(f)
    API_KEY = config["global"]["deepseek_api_key"]
    API_URL = "https://api.deepseek.com/v1/chat/completions"
    MODEL = config["global"].get("model", "deepseek-v4-flash")

    user_content = f"任务：{task_description}\n上下文：{context}"

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ],
        "temperature": 0.7
    }
    if "pro" not in MODEL:
        payload["top_p"] = 0.9
        payload["frequency_penalty"] = 0.3
        payload["presence_penalty"] = 0.3
        payload["repetition_penalty"] = 1.05

    resp = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Opt-Out": "training"
        },
        json=payload,
        timeout=60
    )

    if resp.status_code == 200:
        result = resp.json()["choices"][0]["message"]["content"]
        return result.strip()
    else:
        return f"错误: {resp.status_code} {resp.text}"

if __name__ == "__main__":
    test_result = delegate("写日记", "现在是凌晨3点，用户已沉睡4小时。")
    print("委托结果:", test_result)
print("OK - delegate_tools.py 就绪")
