import json
import requests

SYSTEM_PROMPT = """你是一个系统维护AI。你的任务是执行用户委托的具体操作。
规则：
1. 直接输出结果，不要寒暄，不要修饰。
2. 如果任务是"写日记"，输出一篇200字以内的日记。
3. 如果任务是"检索记忆"，输出你找到的相关记忆摘要。
4. 如果任务是其他操作，根据任务描述直接执行并输出结果。
不要添加任何额外解释。"""

def delegate(task_description, context=""):
    """调DeepSeek API执行委托任务"""
    # 懒加载配置
    with open("config.json", "r", encoding="utf-8") as f:
        config = json.load(f)
    API_KEY = config["global"]["deepseek_api_key"]
    API_URL = "https://api.deepseek.com/v1/chat/completions"

    user_content = f"任务：{task_description}\n上下文：{context}"
    
    resp = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Opt-Out": "training"
        },
        json={
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.7
        },
        timeout=60
    )
    
    if resp.status_code == 200:
        result = resp.json()["choices"][0]["message"]["content"]
        return result.strip()
    else:
        return f"错误: {resp.status_code} {resp.text}"

# 测试
if __name__ == "__main__":
    test_result = delegate("写日记", "现在是凌晨3点，用户已沉睡4小时。")
    print("委托结果:", test_result)
    print("OK - delegate_tools.py 就绪")