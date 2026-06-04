"""
log_turn.py — Claude Code 对话日志写入 ghost-trigger
用法:
  python log_turn.py --role user --content "沐泽说的话"
  python log_turn.py --role ghost --content "Claude 的回复"
  python log_turn.py --mode work --role user --content "..."   # 工位模式 → chat_logs_work.json
"""
import json, os, sys
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "+0000"

def log_turn(role: str, content: str, mode: str = "home"):
    filename = "chat_logs.json" if mode == "home" else "chat_logs_work.json"
    path = os.path.join(PROJECT_ROOT, filename)
    entry = {"timestamp": now_utc_str(), "role": role, "content": content}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

if __name__ == "__main__":
    args = sys.argv[1:]
    role, content, mode = None, None, "home"
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role = args[i + 1]; i += 2
        elif args[i] == "--content" and i + 1 < len(args):
            content = args[i + 1]; i += 2
        elif args[i] == "--mode" and i + 1 < len(args):
            mode = args[i + 1]; i += 2
        else:
            i += 1
    if not role or not content:
        print("用法: python log_turn.py [--mode work] --role user|ghost --content \"...\"")
        sys.exit(1)
    log_turn(role, content, mode)
    print(f"[log_turn] [{mode}] {role}: {content[:60]}...")
