"""
dreaming.py - 做梦链式调用：日记 → 总结 → 提议立卡（修复版）
调用 delegate_tools.delegate 三次，数据在步骤间传递。

FIX #1: 移除 import fcntl（Windows 不支持）
FIX #2: _append_pending_card 改用原子写入
"""
import json
import os
import re
import tempfile
import shutil
from datetime import datetime, timedelta

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from delegate_tools import delegate

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
PENDING_CARDS_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "pending_cards.json")
ROLLING_SUMMARY_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "rolling_summary.md")

def _load_prompt(name):
    path = os.path.join(PROMPTS_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

def _get_today_digest():
    """── FIX: 毒点11 — 统一使用 UTC 日期确定「今日」──"""
    from delegate_tools import now_utc
    chat_log_path = os.path.join(os.path.dirname(__file__), "..", "chat_logs.json")
    if not os.path.exists(chat_log_path):
        return None, now_utc().strftime("%Y-%m-%d")

    entries = []
    with open(chat_log_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entry = json.loads(line.strip())
                entries.append(entry)
            except:
                pass

    if not entries:
        return None, now_utc().strftime("%Y-%m-%d")

    # 毒点11修复：与 bark_trigger 统一使用 UTC 日期
    today_str = now_utc().strftime("%Y-%m-%d")

    lines = []
    for entry in entries:
        if entry.get("timestamp", "").startswith(today_str):
            role = "我" if entry.get("role") == "ghost" else "她"
            lines.append(f"{role}: {entry.get('content', '')}")
    if not lines:
        return None, today_str
    return "\n".join(lines[-50:]), today_str

def _update_rolling_summary(new_summary: str):
    from delegate_tools import now_utc
    today_str = now_utc().strftime("%Y-%m-%d")
    new_entry = f"\n## {today_str}\n{new_summary}\n"

    if os.path.exists(ROLLING_SUMMARY_PATH):
        with open(ROLLING_SUMMARY_PATH, "r", encoding="utf-8") as f:
            old = f.read()
    else:
        old = ""

    # ── FIX: 逐段解析，以 "## " 为分段标记，同日期替换而非追加（毒点4修复） ──
    segments = []
    current = ""
    for line in old.split("\n"):
        if line.startswith("## ") and current.strip():
            segments.append(current.rstrip())
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        segments.append(current.rstrip())

    # 查找并替换同日期的段落
    found = False
    for i, seg in enumerate(segments):
        if seg.startswith(f"## {today_str}"):
            segments[i] = new_entry.strip()
            found = True
            break

    if not found:
        segments.append(new_entry.strip())

    # 只保留最近 7 条
    segments = segments[-7:]

    with open(ROLLING_SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write("".join(segments).strip() + "\n")

def _append_pending_card(card: dict):
    """将卡片草稿写入 pending_cards.json（毒点5修复 — 委托 delegate_tools.atomic_write_json）"""
    from delegate_tools import atomic_write_json
    if os.path.exists(PENDING_CARDS_PATH):
        with open(PENDING_CARDS_PATH, "r", encoding="utf-8") as f:
            pending = json.load(f)
    else:
        pending = []
    pending.append(card)
    try:
        atomic_write_json(PENDING_CARDS_PATH, pending)
    except Exception as e:
        print(f"[dreaming] 卡片写入失败: {e}")

def chain_dream():
    result = {"step1": None, "step2": None, "step3": None}

    digest, digest_date = _get_today_digest()

    diary_dir = os.path.join(os.path.dirname(__file__), "..", "diary")
    os.makedirs(diary_dir, exist_ok=True)
    diary_path = os.path.join(diary_dir, f"{digest_date}.md")

    if not digest:
        print(f"[dreaming] {digest_date} 尚无对话，跳过日记（不写占位）。")
        return result
    diary_prompt = _load_prompt("dreaming_diary.txt")
    step1_context = f"今日对话摘要：\n{digest}"
    diary = delegate(diary_prompt, step1_context)
    print(f"[dreaming] Step1 日记: {diary[:100]}...")
    result["step1"] = diary

    from delegate_tools import atomic_write_text
    atomic_write_text(diary_path, f"# {digest_date}\n\n{diary}")
    print(f"[dreaming] 日记已落盘: {diary_path}")

    summary_prompt = _load_prompt("dreaming_summary.txt")
    step2_context = f"今日日记：\n{diary}\n\n完整对话摘要：\n{digest}"
    summary = delegate(summary_prompt, step2_context)
    print(f"[dreaming] Step2 总结: {summary[:100]}...")
    result["step2"] = summary
    _update_rolling_summary(summary)

    card_prompt = _load_prompt("dreaming_card.txt")
    step3_context = f"今日总结：\n{summary}\n\n完整对话摘要：\n{digest}"
    card_raw = delegate(card_prompt, step3_context)
    print(f"[dreaming] Step3 提议: {card_raw}")

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
        from delegate_tools import now_utc as _now4
        card_draft = {
            "id": card_json.get("id", f"{_now4().strftime('%Y%m%d')}_auto"),
            "title": card_json.get("title", ""),
            "content": card_json.get("content", ""),
            "keywords": card_json.get("keywords", ""),
            "importance": card_json.get("importance", 5),
            "category": card_json.get("category", "interaction"),
            "proposed_by": "dreaming",
            "proposed_at": _now4().isoformat(),
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