"""
dreaming.py - 做梦链式调用：日记 → 总结 → 提议立卡
调用 delegate_tools.delegate 三次，数据在步骤间传递。
"""
import json
import os
import re
import fcntl
from datetime import datetime, timezone, timedelta

# 从项目根目录导入 delegate_tools
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from delegate_tools import delegate

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
PENDING_CARDS_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "pending_cards.json")
ROLLING_SUMMARY_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "rolling_summary.md")

TZ = timezone(timedelta(hours=8))  # 北京时间

def _load_prompt(name):
    path = os.path.join(PROMPTS_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def _get_today_digest():
    """
    从 chat_logs.json 提取当日对话摘要。
    返回字符串，若无当日对话则返回 None。
    """
    chat_log_path = os.path.join(os.path.dirname(__file__), "..", "chat_logs.json")
    if not os.path.exists(chat_log_path):
        return None

    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    lines = []
    with open(chat_log_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                if entry.get("timestamp", "").startswith(today_str):
                    lines.append(f"{entry.get('role','?')}: {entry.get('content','')}")
            except:
                pass
    if not lines:
        return None
    return "\n".join(lines[-50:])  # 取最后50条避免过长

def _update_rolling_summary(new_summary: str):
    """
    将新 summary 片段追加到 memory/rolling_summary.md，
    并只保留最近 7 天的 ## YYYY-MM-DD 段。
    """
    today_str = datetime.now(TZ).strftime("%Y-%m-%d")
    new_entry = f"\n## {today_str}\n{new_summary}\n"

    if os.path.exists(ROLLING_SUMMARY_PATH):
        with open(ROLLING_SUMMARY_PATH, "r", encoding="utf-8") as f:
            old = f.read()
    else:
        old = ""

    # 分割段，保留最近7天
    pattern = r"(## \d{4}-\d{2}-\d{2}.*?)(?=## \d{4}-\d{2}-\d{2}|\Z)"
    segments = re.findall(pattern, old, re.DOTALL)
    segments.append(new_entry)
    segments = segments[-7:]  # 只保留最近7段

    with open(ROLLING_SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write("".join(segments).strip() + "\n")

def _append_pending_card(card: dict):
    """将卡片草稿写入 pending_cards.json"""
    if os.path.exists(PENDING_CARDS_PATH):
        with open(PENDING_CARDS_PATH, "r", encoding="utf-8") as f:
            pending = json.load(f)
    else:
        pending = []
    pending.append(card)
    with open(PENDING_CARDS_PATH, "w", encoding="utf-8") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX)
            json.dump(pending, f, ensure_ascii=False, indent=2)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def chain_dream():
    """
    主入口。执行日记 → 总结 → 立卡三步骤。
    返回 dict 包含每步结果。
    """
    result = {"step1": None, "step2": None, "step3": None}

    # Step 1: 写日记
    digest = _get_today_digest()
    if not digest:
        print("[dreaming] 今日无对话，跳过 dreaming。")
        return result

    diary_prompt = _load_prompt("dreaming_diary.txt")
    step1_context = f"今日对话摘要：\n{digest}"
    diary = delegate(diary_prompt, step1_context)
    print(f"[dreaming] Step1 日记: {diary[:100]}...")
    result["step1"] = diary

    # ===================== DS 要求追加的落盘代码 =====================
    # 日记落盘
    diary_dir = os.path.join(os.path.dirname(__file__), "..", "diary")
    os.makedirs(diary_dir, exist_ok=True)
    diary_path = os.path.join(diary_dir, f"{datetime.now(TZ).strftime('%Y-%m-%d')}.md")
    with open(diary_path, "w", encoding="utf-8") as f:
        f.write(f"# {datetime.now(TZ).strftime('%Y-%m-%d')}\n\n{diary}")
    print(f"[dreaming] 日记已落盘: {diary_path}")
    # =================================================================

    # Step 2: 总结
    summary_prompt = _load_prompt("dreaming_summary.txt")
    step2_context = f"今日日记：\n{diary}"
    summary = delegate(summary_prompt, step2_context)
    print(f"[dreaming] Step2 总结: {summary[:100]}...")
    result["step2"] = summary

    # 更新滚动总结
    _update_rolling_summary(summary)

    # Step 3: 提议立卡
    card_prompt = _load_prompt("dreaming_card.txt")
    step3_context = f"今日总结：\n{summary}"
    card_raw = delegate(card_prompt, step3_context)
    print(f"[dreaming] Step3 提议: {card_raw}")

    # 解析 JSON
    try:
        card_json = json.loads(card_raw)
    except:
        json_match = re.search(r'\{.*\}', card_raw, re.DOTALL)
        if json_match:
            try:
                card_json = json.loads(json_match.group())
            except:
                card_json = {"action": "skip"}
        else:
            card_json = {"action": "skip"}

    if card_json.get("action") == "create":
        # 构造 pending 卡片
        card_draft = {
            "id": card_json.get("id", f"{datetime.now(TZ).strftime('%Y%m%d')}_auto"),
            "title": card_json.get("title", ""),
            "content": card_json.get("content", ""),
            "keywords": card_json.get("keywords", ""),
            "importance": card_json.get("importance", 5),
            "category": card_json.get("category", "interaction"),
            "proposed_by": "dreaming",
            "proposed_at": datetime.now(TZ).isoformat(),
            "review_status": "pending"
        }
        _append_pending_card(card_draft)
        result["step3"] = card_draft
        print(f"[dreaming] 卡片草稿已写入 pending_cards.json: {card_draft['id']}")
    else:
        result["step3"] = {"action": "skip"}
        print("[dreaming] 无需立卡，跳过。")

    return result

if __name__ == "__main__":
    chain_dream()
    print("OK - dreaming.py 就绪")