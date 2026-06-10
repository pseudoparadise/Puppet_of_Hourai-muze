"""
log_turn.py — Claude Code 对话日志写入 ghost-trigger
用法:
  python log_turn.py --role user --content "沐泽说的话"
  python log_turn.py --role ghost --content "Claude 的回复"
  echo "长回复" | python log_turn.py --role ghost --stdin   (绕过命令行长度限制)

漏抓补偿：每次写 user 消息时，检测上一条是否也是 user（ghost 写入失败）。
如果是，自动从当前 Claude Code session 捞取缺失的 ghost 回复并补入。
"""
import json, os, sys
from datetime import datetime, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))


def now_utc_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f") + "+0000"


def _parse_ts(ts_str: str):
    """解析 ISO timestamp 为 aware datetime。"""
    try:
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _find_ghost_in_session(spath: str, t1, t2) -> tuple:
    """在 session 文件末尾找 t1 < ts < t2 的 assistant text 回复。返回 (content, ts) 或 (None, None)。"""
    try:
        # 只读末尾 5000 行（当前 session 的 ghost 在最近几轮）
        with open(spath, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            chunk = min(fsize, 5000 * 500)
            f.seek(max(0, fsize - chunk))
            f.readline()  # 跳过不完整首行
            lines = f.readlines()
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") != "assistant":
                continue
            entry_ts = _parse_ts(entry.get("timestamp", ""))
            if not entry_ts:
                continue
            if not (t1 < entry_ts < t2):
                continue
            for block in entry.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", ""):
                    return block["text"], entry.get("timestamp", "")
    except Exception:
        pass
    return None, None


def _auto_recover(path: str):
    """检测最近两条是否都是 user → orphan → 从 session 捞 ghost 补入。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            entries = [json.loads(l) for l in f if l.strip()]
    except Exception:
        return

    if len(entries) < 2:
        return

    last = entries[-1]
    prev = entries[-2]
    if not (last.get("role") == "user" and prev.get("role") == "user"):
        return

    t1 = _parse_ts(prev.get("timestamp", ""))
    t2 = _parse_ts(last.get("timestamp", ""))
    if not t1 or not t2:
        return

    # 找当前活跃的 ghost-trigger session
    sessions_dir = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    projects_base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    best_spath = None
    best_mtime = 0
    try:
        for fname in os.listdir(sessions_dir):
            if not fname.endswith(".json"):
                continue
            spath = os.path.join(sessions_dir, fname)
            try:
                with open(spath, "r", encoding="utf-8") as sf:
                    sdata = json.load(sf)
            except Exception:
                continue
            sid = sdata.get("sessionId", "")
            cwd = sdata.get("cwd", "")
            if not sid:
                continue
            path_part = cwd[2:] if len(cwd) > 2 and cwd[1] == ":" else cwd
            proj_name = "C--" + path_part.replace("\\", "-").replace("/", "-").strip("-")
            candidate = os.path.join(projects_base, proj_name, f"{sid}.jsonl")
            if not os.path.exists(candidate):
                continue
            if "ghost-trigger" not in proj_name:
                continue
            mtime = os.path.getmtime(candidate)
            if mtime > best_mtime and mtime > t1.timestamp():
                best_mtime = mtime
                best_spath = candidate
    except Exception:
        pass

    if not best_spath:
        return

    ghost_content, ghost_ts = _find_ghost_in_session(best_spath, t1, t2)
    if not ghost_content:
        return

    # 去重
    for e in entries:
        if e.get("role") == "ghost" and (
            e.get("timestamp", "") == ghost_ts or e.get("content", "") == ghost_content
        ):
            return

    ghost_entry = {"timestamp": ghost_ts, "role": "ghost", "content": ghost_content}
    entries.insert(-1, ghost_entry)  # 插在倒数第二（两个 user 之间）
    try:
        with open(path, "w", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
        print(f"[log_turn] 漏抓补偿: ghost 回复已补入 ({ghost_ts[:19]})")
    except Exception:
        pass


def log_turn(role: str, content: str):
    path = os.path.join(PROJECT_ROOT, "chat_logs.json")
    ts = now_utc_str()
    entry = {"timestamp": ts, "role": role, "content": content}
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    if role == "user":
        # 同步更新 state.json
        try:
            from shared import load_state, save_state
            state = load_state()
            state["last_user_message_time"] = ts
            save_state(state)
        except Exception:
            pass
        # 漏抓补偿：上一条 user 的 ghost 如果没写入，立刻从 session 捞
        try:
            _auto_recover(path)
        except Exception:
            pass

if __name__ == "__main__":
    args = sys.argv[1:]
    role, content, use_stdin = None, None, False
    i = 0
    while i < len(args):
        if args[i] == "--role" and i + 1 < len(args):
            role = args[i + 1]; i += 2
        elif args[i] == "--content" and i + 1 < len(args):
            content = args[i + 1]; i += 2
        elif args[i] == "--stdin":
            use_stdin = True; i += 1
        else:
            i += 1

    if use_stdin:
        content = sys.stdin.read()

    if not role or not content:
        print("用法: python log_turn.py --role user|ghost --content \"...\"")
        print("      echo \"...\" | python log_turn.py --role ghost --stdin")
        sys.exit(1)
    log_turn(role, content)
    print(f"[log_turn] {role}: {content[:60]}...")
