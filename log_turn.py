"""
log_turn.py — Claude Code 对话日志写入 ghost-trigger
用法:
  python log_turn.py --role user --content "沐泽说的话"
  python log_turn.py --role ghost --content "Claude 的回复"
工位日志由 polling_loop 自动从 Claude Code session 提取，无需手动调用。
"""
import json, os, sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "+0000"

def log_turn(role: str, content: str):
    path = os.path.join(PROJECT_ROOT, "chat_logs.json")
    ts = now_utc_str()
    entry = {"timestamp": ts, "role": role, "content": content}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    # 同步更新 state.json，让 bark_trigger 能读到最新活跃时间
    if role == "user":
        try:
            from shared import load_state, save_state
            state = load_state()
            state["last_user_message_time"] = ts
            save_state(state)
        except Exception:
            pass

if __name__ == "__main__":
    args = sys.argv[1:]
    role, content = None, None
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role = args[i + 1]; i += 2
        elif args[i] == "--content" and i + 1 < len(args):
            content = args[i + 1]; i += 2
        else:
            i += 1
    if not role or not content:
        print("用法: python log_turn.py --role user|ghost --content \"...\"")
        sys.exit(1)
    log_turn(role, content)
    print(f"[log_turn] {role}: {content[:60]}...")
