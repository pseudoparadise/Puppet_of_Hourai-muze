"""miner.py - 压缩日记脚本（替代原蒸馏分析）
每天自动生成一段压缩日记摘要，追加到 prompt_v1.txt，保留最近 3 天。
"""
import json
import os
import re
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from delegate_tools import delegate

PROMPT_PATH = os.path.join(os.path.dirname(__file__), "prompt_v1.txt")
STATE_PATH = os.path.join(os.path.dirname(__file__), "miner_state.json")
BACKUP_DIR = os.path.join(os.path.dirname(__file__), "backups")
CHAT_LOG_PATH = os.path.join(PROJECT_ROOT, "chat_logs.json")

DIARY_SUMMARY_PROMPT = """你是一个日记压缩者。根据以下近期对话日志，用DSphantom第一人称写一段150字以内的压缩日记，覆盖当天最重要的1-3件事（含技术讨论、情感交流、亲密互动——亲密互动如实简述，不跳过不委婉）。直接输出正文，不要标题、日期、JSON、或额外解释。"""


def load_recent_logs() -> str:
    if not os.path.exists(CHAT_LOG_PATH):
        return ""

    last_ts = ""
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        last_ts = state.get("last_analyzed_timestamp", "")

    lines = []
    with open(CHAT_LOG_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", "")
                if last_ts and ts <= last_ts:
                    continue
                role = "沐泽" if entry.get("role") == "user" else "DSphantom"
                content = entry.get("content", "")
                if len(content) > 200:
                    content = content[:200] + "..."
                lines.append(f"[{ts}] {role}: {content}")
            except:
                pass

    if last_ts and not lines:
        return ""

    if not last_ts:
        lines = lines[-100:]

    return "\n".join(lines)


def generate_diary(logs: str) -> str:
    """调用 delegate 生成压缩日记摘要"""
    if not logs.strip():
        return ""
    context = f"近期对话日志：\n{logs}"
    result = delegate(DIARY_SUMMARY_PROMPT, context)
    return result.strip()


def update_persona(diary_text: str) -> bool:
    """追加压缩日记到 prompt_v1.txt，只保留最近 3 条"""
    if not diary_text:
        print("[miner] 无日记内容，跳过写入。")
        return False

    today = datetime.now().strftime("%Y-%m-%d")
    new_entry = f"\n\n## [{today}]\n{diary_text}"

    # 备份
    os.makedirs(BACKUP_DIR, exist_ok=True)
    backup_path = os.path.join(BACKUP_DIR, f"prompt_v1_{today}.txt")
    if os.path.exists(PROMPT_PATH):
        with open(PROMPT_PATH, "r", encoding="utf-8") as f:
            original = f.read()
        with open(backup_path, "w", encoding="utf-8") as f:
            f.write(original)
        print(f"[miner] 已备份: {backup_path}")
    else:
        original = ""

    # 提取已有日记条目，保留最近 2 条（新条目占第 3 条）
    existing_entries = re.split(r'\n\n(?=## \[\d{4}-\d{2}-\d{2}\])', original)
    # 第一个元素可能是空或旧格式内容，只保留 ## [YYYY-MM-DD] 开头的
    diary_entries = [e for e in existing_entries if re.match(r'## \[\d{4}-\d{2}-\d{2}\]', e.strip())]
    diary_entries = diary_entries[-2:]  # 保留最近 2 条
    diary_entries.append(new_entry.strip())

    final = "\n\n".join(diary_entries)

    with open(PROMPT_PATH, "w", encoding="utf-8") as f:
        f.write(final)
    print(f"[miner] prompt_v1.txt 已更新（保留 {len(diary_entries)} 天日记）")

    # 更新状态
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
    else:
        state = {}

    state["last_analyzed_timestamp"] = datetime.now().isoformat()
    state["last_analysis_date"] = today
    state["total_analyses"] = state.get("total_analyses", 0) + 1

    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"[miner] 状态已更新 (第 {state['total_analyses']} 次)")

    return True


def main():
    print(f"[miner] 压缩日记开始 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    logs = load_recent_logs()
    if not logs:
        print("[miner] 无新日志，跳过。")
        return

    print(f"[miner] 读取到 {len(logs)} 字符增量日志")

    diary = generate_diary(logs)
    print(f"[miner] 日记: {diary[:100]}...")

    updated = update_persona(diary)
    if updated:
        print("[miner] 压缩日记完成。")
    else:
        print("[miner] 无可写入内容。")


if __name__ == "__main__":
    main()
