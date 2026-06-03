"""
log_turn.py — Claude Code 对话日志写入 ghost-trigger 的 chat_logs.json
用法:
  python log_turn.py --role user --content "沐泽说的话"
  python log_turn.py --role ghost --content "Claude 的回复"
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(PROJECT_ROOT, "chat_logs.json")

def now_utc_str():
    dt = datetime.now(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f") + "+0000"

def log_turn(role: str, content: str):
    entry = {
        "timestamp": now_utc_str(),
        "role": role,
        "content": content,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    args = sys.argv[1:]
    role = None
    content = None
    for i, a in enumerate(args):
        if a == "--role" and i + 1 < len(args):
            role = args[i + 1]
        elif a == "--content" and i + 1 < len(args):
            content = args[i + 1]
    if not role or not content:
        print("用法: python log_turn.py --role user|ghost --content \"...\"")
        sys.exit(1)
    log_turn(role, content)
    print(f"[log_turn] {role}: {content[:60]}...")
