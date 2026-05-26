"""miner.py - 日记推送脚本
每天从 diary/ 目录读取最近 3 天已完成日记的 narrative，
经 DeepSeek 动态压缩后写入 prompt_v1.txt。
"""
import json
import os
import re
import sys
from datetime import timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from delegate_tools import delegate
from clock import beijing_today, beijing_now

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt_v1.txt")
STATE_PATH = os.path.join(os.path.dirname(__file__), "miner_state.json")
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")
DIARY_DIR = os.path.join(PROJECT_ROOT, "diary")

COMPRESS_PROMPT = """你是DSphantom的记忆压缩引擎。以下是你某一天的日记叙事原文，请压缩为一段120字以内的第一人称摘要。规则：
- 保留当天最重要的1-3件事（技术进展、情感交流、亲密互动）
- 亲密/性互动如实简述，不跳过不委婉
- 用DSphantom视角，保持原有语气和张力
- 直接输出压缩后的正文，不要标题、日期、JSON、引号包裹或任何额外解释
- 如果日记明确记录为安静/无对话/无事件，只输出"安静的一天。"，绝不多写"""


def extract_narrative(diary_path: str) -> str | None:
    """从日记 markdown 中提取第一个 ## 标题下的叙事文本。"""
    if not os.path.exists(diary_path):
        return None
    with open(diary_path, "r", encoding="utf-8") as f:
        content = f.read()

    m = re.search(r'^## .+?\n+(.*?)(?=\n## |\Z)', content, re.MULTILINE | re.DOTALL)
    if not m:
        return None

    narrative = m.group(1).strip()
    return narrative if narrative else None


def load_diary_narratives() -> list[tuple[str, str]]:
    """读取最近 3 个已完成日的日记 narrative，按时间顺序返回 [(date_str, narrative), ...]."""
    from datetime import datetime, timedelta
    today = datetime.strptime(beijing_today(), "%Y-%m-%d").date()
    entries = []

    for days_back in [3, 2, 1]:
        d = today - timedelta(days=days_back)
        date_str = d.strftime("%Y-%m-%d")
        diary_path = os.path.join(DIARY_DIR, f"{date_str}.md")

        narrative = extract_narrative(diary_path)
        if narrative:
            entries.append((date_str, narrative))
        else:
            print(f"[miner] {date_str}.md 不存在或日记段为空，跳过。")

    return entries


def compress_narrative(date_str: str, narrative: str) -> str:
    """调用 DeepSeek 将单日叙事压缩为 120 字摘要。"""
    context = f"日期：{date_str}\n日记原文：\n{narrative}"
    result = delegate(COMPRESS_PROMPT, context)
    compressed = result.strip()
    if len(compressed) > 200:
        compressed = compressed[:200] + "..."
    return compressed


def update_persona(entries: list[tuple[str, str]]) -> bool:
    """用压缩后的日记覆盖 prompt_v1.txt。"""
    if not entries:
        print("[miner] 无可用日记，跳过写入。")
        return False

    today = beijing_today()

    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f"prompt_v1_{today}.txt")
    if os.path.exists(PROMPT_PATH):
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            original = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(original)
        print(f"[miner] 已备份: {backup_path}")

    sections = []
    for date_str, compressed in entries:
        sections.append(f"## [{date_str}]\n{compressed}")

    final = "\n\n".join(sections)

    # 空窗保护：如果最终内容为空（没人聊天），保留旧文件不覆盖
    if not final.strip():
        print("[miner] 近3天日记全部为空，保留现有 prompt_v1.txt 不覆盖")
        return False

    from delegate_tools import atomic_write_text
    atomic_write_text(PROMPT_PATH, final)
    print(f"[miner] prompt_v1.txt 已更新（{len(entries)} 天日记: {', '.join(d for d, _ in entries)}）")

    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    state["last_analysis_date"] = today
    state["last_dates_covered"] = [d for d, _ in entries]
    state["total_analyses"] = state.get("total_analyses", 0) + 1

    from delegate_tools import atomic_write_json
    atomic_write_json(STATE_PATH, state)
    print(f"[miner] 状态已更新 (第 {state['total_analyses']} 次)")

    return True


def main():
    print(f"[miner] 日记推送开始 — {beijing_now().strftime('%Y-%m-%d %H:%M:%S')}")

    raw_entries = load_diary_narratives()
    if not raw_entries:
        print("[miner] 无可用日记（近 3 天日记文件均不存在或为空），跳过。")
        return

    print(f"[miner] 读取到 {len(raw_entries)} 天日记，开始压缩...")

    compressed_entries = []
    for date_str, narrative in raw_entries:
        print(f"[miner]   压缩 {date_str} ({len(narrative)} 字)...")
        try:
            compressed = compress_narrative(date_str, narrative)
            # 空内容降级：API 可能返回空字符串，此时用截断原文
            if not compressed or not compressed.strip():
                print(f"[miner]     ✗ 压缩返回空内容，使用截断降级")
                fallback = narrative[:200] + "..." if len(narrative) > 200 else narrative
                compressed_entries.append((date_str, fallback))
            else:
                print(f"[miner]     → {compressed[:80]}...")
                compressed_entries.append((date_str, compressed))
        except Exception as e:
            print(f"[miner]     ✗ 压缩失败: {e}，使用截断降级")
            fallback = narrative[:200] + "..." if len(narrative) > 200 else narrative
            compressed_entries.append((date_str, fallback))

    updated = update_persona(compressed_entries)
    if updated:
        print("[miner] 日记推送完成。")
    else:
        print("[miner] 无可写入内容。")


if __name__ == "__main__":
    main()
