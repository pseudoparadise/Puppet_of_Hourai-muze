"""
work_log.py — 工位模式每日工作日志
用法:
  python work_log.py "修了 retriever 的 category filter bug"
  python work_log.py --today  查看今天的工位日志
  python work_log.py --from-chat [date]  从 chat_logs_work.json 用 DS flash 提取工作条目
  python work_log.py --from-claude-sessions [date]  从 Claude Code session JSONL 提取 → 写 diary
"""
import json
import os
import sys
import glob as _glob
from datetime import datetime, timezone, timedelta

from clock import utc_ts_to_beijing_date, BJT

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_HOME = os.environ.get("USERPROFILE") or os.environ.get("HOME") or os.path.expanduser("~")
CLAUDE_PROJECT_DIRS = [
    os.path.join(_HOME, ".claude", "projects", "C--Users-23807-ghost-trigger"),
    os.path.join(_HOME, ".claude", "projects", "C--Users-23807"),
    os.path.join(_HOME, ".claude", "projects", "C--Users-23807-Desktop---"),
]
CLAUDE_PROJECT_DIRS = [d for d in CLAUDE_PROJECT_DIRS if os.path.isdir(d)]
if not CLAUDE_PROJECT_DIRS:
    CLAUDE_PROJECT_DIRS = [
        "C:/Users/23807/.claude/projects/C--Users-23807-ghost-trigger",
        "C:/Users/23807/.claude/projects/C--Users-23807",
        "C:/Users/23807/.claude/projects/C--Users-23807-Desktop---",
    ]
    CLAUDE_PROJECT_DIRS = [d for d in CLAUDE_PROJECT_DIRS if os.path.isdir(d)]

def beijing_now():
    return datetime.now(timezone(timedelta(hours=8)))

def work_log_path(date_str: str = None):
    if date_str is None:
        date_str = beijing_now().strftime("%Y-%m-%d")
    return os.path.join(PROJECT_ROOT, "diary", "work", f"{date_str}_work.md")

def append_entry(description: str):
    path = work_log_path()
    ts = beijing_now().strftime("%H:%M")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"- [{ts}] {description}\n")
    print(f"[work_log] 已写入: {ts} {description[:50]}...")

def show_day(date_str):
    path = work_log_path(date_str)
    if not os.path.exists(path):
        return False
    with open(path, "r", encoding="utf-8") as f:
        content = f.read().strip()
    if content:
        print(f"[{date_str}]")
        print(content)
        return True
    return False

def show_today():
    today = beijing_now().strftime("%Y-%m-%d")
    yesterday = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")
    shown = show_day(today)
    if not shown and show_day(yesterday):
        print(f"\n(今天还没有工位日志，以上为昨天 {yesterday} 的内容)")
    elif not shown:
        print("(今天还没有工位日志)")


def _extract_text_from_message(msg: dict) -> str:
    content = msg.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return " ".join(parts)
    return ""

def from_claude_sessions(date_str: str = None):
    """从 Claude Code session JSONL 文件中增量提取指定日期的聊天，写入 chat_logs_work.json，
    然后调用 from_chat() 生成工作日记条目。记录 last_ts 游标，下次只拉增量。"""
    if date_str is None:
        date_str = beijing_now().strftime("%Y-%m-%d")

    if not CLAUDE_PROJECT_DIRS:
        print(f"[work_log] 无可用 Claude session 目录")
        return 0

    state = _load_extract_state()
    day_state = state.get(date_str, {})
    cursor = day_state.get("chat_cursor", "")  # 上次读到的时间戳

    all_entries = []
    for project_dir in CLAUDE_PROJECT_DIRS:
        jsonl_files = sorted(_glob.glob(os.path.join(project_dir, "*.jsonl")))
        if not jsonl_files:
            continue
        for jf in jsonl_files:
            try:
                with open(jf, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        ts = entry.get("timestamp", "")
                        if utc_ts_to_beijing_date(ts) != date_str:
                            continue
                        if cursor and ts <= cursor:
                            continue
                        etype = entry.get("type", "")
                        text = ""
                        if etype in ("user", "assistant"):
                            text = _extract_text_from_message(entry.get("message", {}))
                        if not text:
                            continue
                        role = "ghost" if etype == "assistant" else "user"
                        all_entries.append({"timestamp": ts, "role": role, "content": text})
            except Exception as e:
                print(f"[work_log] 读 session 文件 {os.path.basename(jf)} 出错: {e}")
                continue

    entries = sorted(all_entries, key=lambda x: x["timestamp"])
    newest_ts = cursor
    for e in entries:
        if e["timestamp"] > newest_ts:
            newest_ts = e["timestamp"]

    if not entries:
        print(f"[work_log] {date_str} 无新对话 (cursor={cursor[:19] if cursor else '起始'})")
        return 0

    print(f"[work_log] {date_str} 增量 {len(entries)} 条 (cursor={cursor[:19] if cursor else '起始'} -> {newest_ts[:19]})")

    chat_path = os.path.join(PROJECT_ROOT, "chat_logs_work.json")
    os.makedirs(os.path.dirname(chat_path), exist_ok=True)
    written = 0
    with open(chat_path, "a", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            written += 1

    print(f"[work_log] chat_logs_work.json 新增 {written} 条")

    day_state["chat_cursor"] = newest_ts
    state[date_str] = day_state
    _save_extract_state(state)

    diary_written = from_chat(date_str, force=True)
    return diary_written

EXTRACT_COOLDOWN = 12 * 3600  # 同一天两次提取之间至少 12 小时

def _load_extract_state():
    path = os.path.join(PROJECT_ROOT, "memory", "work_log_state.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def _save_extract_state(state: dict):
    path = os.path.join(PROJECT_ROOT, "memory", "work_log_state.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f)

def from_chat(date_str: str = None, force: bool = False):
    """从 chat_logs_work.json 读取当日 ghost 消息，用 DS flash 提取工作任务，写入日记。
    同一天最多提取两次（间隔 >= 12h），避免反复烧 token。
    force=True 跳过冷却（控制台手动触发）。"""
    if date_str is None:
        date_str = beijing_now().strftime("%Y-%m-%d")

    state = _load_extract_state()
    last = state.get(date_str, {})
    last_ts = last.get("at", "")
    if last_ts and not force:
        last_dt = datetime.fromisoformat(last_ts)
        elapsed = (beijing_now().replace(tzinfo=None) - last_dt).total_seconds()
        if elapsed < EXTRACT_COOLDOWN:
            print(f"[work_log] {date_str} 距上次提取仅 {elapsed/3600:.1f}h，跳过")
            return last.get("count", 0)

    chat_path = os.path.join(PROJECT_ROOT, "chat_logs_work.json")
    if not os.path.exists(chat_path):
        print(f"[work_log] chat_logs_work.json 不存在，跳过")
        return 0

    ghost_texts = []
    with open(chat_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = entry.get("timestamp", "")
            if utc_ts_to_beijing_date(ts) != date_str:
                continue
            if entry.get("role") != "ghost":
                continue
            content = entry.get("content", "").strip()
            if content and len(content) > 30:
                bj_time = utc_ts_to_beijing_date(ts)  # same as date_str
                try:
                    t = ts.strip()
                    if t.endswith("Z"):
                        t = t[:-1] + "+00:00"
                    dt = datetime.fromisoformat(t)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    hm = dt.astimezone(BJT).strftime("%H:%M")
                except Exception:
                    hm = "??:??"
                ghost_texts.append((hm, content[:600]))

    if not ghost_texts:
        print(f"[work_log] {date_str} 无 ghost 消息")
        return 0

    BATCH_SIZE = 30
    task = (
        "从以上 AI 回复记录中提取今天实际完成的技术工作任务。\n"
        "规则：\n"
        "- 只提取已经做完的事，不提取讨论/计划/提问\n"
        "- 每行一条：\"- [做了什么]【HH:MM】\"，时间从对应记录的 @HH:MM 获取\n"
        "- 每条 10-25 字，具体到文件名/函数名/参数名\n"
        "- 不相关的闲聊、打招呼、确认收到 — 全部跳过\n"
        "- 输出纯文本，不要任何其他内容"
    )

    from delegate_tools import delegate
    all_lines = []
    for batch_start in range(0, len(ghost_texts), BATCH_SIZE):
        batch = ghost_texts[batch_start:batch_start + BATCH_SIZE]
        context_parts = []
        for i, (hm, text) in enumerate(batch, 1):
            context_parts.append(f"[{i} @{hm}] {text}")
        context = "\n".join(context_parts)

        batch_label = f"{batch[0][0]}-{batch[-1][0]}"
        print(f"[work_log] {date_str} batch {batch_start//BATCH_SIZE+1} [{batch_label}] 发送 {len(batch)} 条到 DS flash...")
        result = delegate(task, context)

        if not result or result.startswith("错误"):
            print(f"[work_log] batch {batch_start//BATCH_SIZE+1} DS 返回异常: {result[:200] if result else '空'}")
            continue

        batch_lines = [l.strip() for l in result.split("\n") if l.strip().startswith("- ")]
        all_lines.extend(batch_lines)

    if not all_lines:
        print(f"[work_log] DS 未提取到任何工作条目")
        return 0

    lines = all_lines

    if not lines:
        print(f"[work_log] DS 未提取到工作条目:\n{result[:300]}")
        return 0

    path = work_log_path(date_str)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    import re as _re
    _time_tag = _re.compile(r'【\d{2}:\d{2}】$')
    def _dedup_key(s: str) -> str:
        return _time_tag.sub('', s.strip())
    seen = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                seen.add(_dedup_key(line))
    written = 0
    with open(path, "a", encoding="utf-8") as f:
        for line in lines:
            if _dedup_key(line) in seen:
                continue
            seen.add(_dedup_key(line))
            f.write(line + "\n")
            written += 1

    prev = state.get(date_str, {})
    state[date_str] = {
        "at": beijing_now().replace(tzinfo=None).isoformat(),
        "count": written,
        "msg_count": len(ghost_texts),
        "chat_cursor": prev.get("chat_cursor", ""),
    }
    _save_extract_state(state)

    print(f"[work_log] {date_str} DS flash 提取 {written} 条工作记录")
    return written

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python work_log.py \"工作内容\" | --today | --yesterday | --from-chat [date] | --from-claude-sessions [date]")
        sys.exit(1)
    arg = sys.argv[1]
    if arg == "--today":
        show_today()
    elif arg == "--yesterday":
        yd = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        show_day(yd)
    elif arg == "--from-chat":
        date_arg = sys.argv[2] if len(sys.argv) > 2 else None
        from_chat(date_arg)
    elif arg == "--from-claude-sessions":
        date_arg = sys.argv[2] if len(sys.argv) > 2 else None
        from_claude_sessions(date_arg)
    else:
        append_entry(" ".join(sys.argv[1:]))
