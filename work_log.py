"""
work_log.py — 工位模式每日工作日志
用法:
  python work_log.py "修了 retriever 的 category filter bug"
  python work_log.py --today  查看今天的工位日志
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

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

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python work_log.py \"工作内容\"   或   python work_log.py --today")
        sys.exit(1)
    arg = sys.argv[1]
    if arg == "--today":
        show_today()
    elif arg == "--yesterday":
        yd = (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        show_day(yd)
    else:
        append_entry(" ".join(sys.argv[1:]))
