"""
phantom_cli.py — Claude Code ↔ phantom-trigger 桥梁
用法:
  python phantom_cli.py cards --cat deep_talks --limit 5    查卡片
  python phantom_cli.py cards --id <card_id>                查单张
  python phantom_cli.py chat --recent 20                    查聊天
  python phantom_cli.py chat --search "关键词"               搜索聊天
  python phantom_cli.py diary --days 3                      查日记
  python phantom_cli.py status                              总览
  python phantom_cli.py recall "查询文本"                    模拟检索
  python phantom_cli.py links <card_id>                     查 link 邻居
  python phantom_cli.py reslog --last 20                    查划卡审计日志
"""
import json
import os
import sys
import sqlite3

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)


def _db():
    conn = sqlite3.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
    conn.row_factory = sqlite3.Row
    return conn


def cmd_cards(args):
    conn = _db()
    c = conn.cursor()

    if "--id" in args:
        idx = args.index("--id")
        cid = args[idx + 1] if idx + 1 < len(args) else ""
        c.execute("SELECT * FROM cards WHERE id=?", (cid,))
        row = c.fetchone()
        if row:
            d = dict(row)
            d.pop("embedding", None)
            print(json.dumps(d, ensure_ascii=False, indent=2))
        else:
            print(f"卡片 {cid} 不存在")
        conn.close()
        return

    cat = None
    if "--cat" in args:
        idx = args.index("--cat")
        cat = args[idx + 1] if idx + 1 < len(args) else None

    limit = 10
    if "--limit" in args:
        idx = args.index("--limit")
        limit = int(args[idx + 1]) if idx + 1 < len(args) else 10

    resolved = "all"
    if "--active" in args:
        resolved = "0"

    sql = "SELECT id, title, category, importance, valence, arousal, chord, usage_count, resolved, created_at FROM cards WHERE review_status='final'"
    params = []
    if cat:
        sql += " AND category=?"
        params.append(cat)
    if resolved == "0":
        sql += " AND resolved=0"

    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    c.execute(sql, params)

    for row in c.fetchall():
        d = dict(row)
        flags = []
        if d["resolved"]: flags.append("✓")
        if d["importance"] >= 8: flags.append("★")
        flag_str = " ".join(flags)
        print(f"[{d['category']:<15}] {flag_str:<6} imp={d['importance']} | {d['title'][:50]}")
        print(f"  id: {d['id']} | v={d['valence']:.2f} a={d['arousal']:.2f} used={d['usage_count']}")
        if d["chord"]:
            print(f"  chord: {d['chord']}")
    conn.close()


def cmd_chat(args):
    log_path = os.path.join(PROJECT_ROOT, "chat_logs.json")
    if not os.path.exists(log_path):
        print("chat_logs.json 不存在")
        return

    recent = 20
    if "--recent" in args:
        idx = args.index("--recent")
        recent = int(args[idx + 1]) if idx + 1 < len(args) else 20

    search = None
    if "--search" in args:
        idx = args.index("--search")
        search = args[idx + 1] if idx + 1 < len(args) else None

    entries = []
    with open(log_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                entries.append(json.loads(line.strip()))
            except json.JSONDecodeError:
                pass

    if search:
        entries = [e for e in entries if search.lower() in str(e.get("content", "")).lower()]

    for entry in entries[-recent:]:
        role = entry.get("role", "?")
        ts = entry.get("timestamp", "")[:19]
        content = str(entry.get("content", ""))[:120]
        chord = entry.get("chord", "")
        extra = f" [{chord}]" if chord else ""
        print(f"[{ts}] {role}{extra}: {content}")


def cmd_diary(args):
    days = 3
    if "--days" in args:
        idx = args.index("--days")
        days = int(args[idx + 1]) if idx + 1 < len(args) else 3

    from datetime import datetime, timedelta
    diary_dir = os.path.join(PROJECT_ROOT, "diary")
    for d_offset in range(1, days + 1):
        d = (datetime.now() - timedelta(days=d_offset)).strftime("%Y-%m-%d")
        md = os.path.join(diary_dir, f"{d}.md")
        ev = os.path.join(diary_dir, f"{d}_events.json")
        if os.path.exists(md):
            size = os.path.getsize(md)
            print(f"  {d}: diary ({size}B)")
        if os.path.exists(ev):
            with open(ev, "r", encoding="utf-8") as f:
                ev_data = json.load(f)
            completions = ev_data.get("completions", [])
            eis = ev_data.get("eisenhower", {})
            if completions:
                print(f"    完成: {len(completions)} 项")
            for label, key in [("重要且紧急", "important_urgent"), ("重要不紧急", "important_not_urgent")]:
                items = eis.get(key, [])
                if items:
                    print(f"    {label}: {len(items)} 项")
    if not any(os.path.exists(os.path.join(diary_dir, f"{(datetime.now() - timedelta(days=d)).strftime('%Y-%m-%d')}.md")) for d in range(1, days + 1)):
        print("  无日记")


def cmd_status():
    conn = _db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final'")
    final = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final' AND resolved=0")
    active = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM cards WHERE review_status='final' AND embedding IS NOT NULL")
    vec = c.fetchone()[0]

    c.execute("SELECT category, COUNT(*) FROM cards WHERE review_status='final' GROUP BY category ORDER BY COUNT(*) DESC")
    cats = c.fetchall()

    # link count
    try:
        c.execute("SELECT COUNT(*) FROM card_links")
        links = c.fetchone()[0]
    except Exception:
        links = 0

    # pending
    pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
    pending = 0
    if os.path.exists(pending_path):
        with open(pending_path, "r", encoding="utf-8") as f:
            pending = len(json.load(f))

    conn.close()

    print(f"定稿卡片: {final} (活跃 {active}, 已解决 {final - active})")
    print(f"有向量: {vec}")
    print(f"待审核: {pending}")
    print(f"Link 边: {links}")
    print()
    print("分类分布:")
    for cat, cnt in cats:
        bar = "█" * min(cnt, 30)
        print(f"  {cat:<18} {cnt:>4} {bar}")


def cmd_recall(args):
    query = " ".join(args).strip() if args else ""
    if not query:
        print("用法: python phantom_cli.py recall 查询文本")
        return
    sys.path.insert(0, PROJECT_ROOT)
    sys.path.insert(0, os.path.join(PROJECT_ROOT, "memory"))
    from memory.retriever import retrieve
    cards = retrieve(query, top_k=5, trace_tag="phantom_cli")
    for c in cards:
        print(f"  [{c['category']}] {c['title']} (score={c['score']:.1f})")
        print(f"    id={c['id']} hits={c['hit_count']} dist={c['distance']}")


def cmd_links(args):
    if not args:
        print("用法: python phantom_cli.py links <card_id>")
        return
    card_id = args[0]
    try:
        from memory.linker import get_linked_with_similarity
        neighbors = get_linked_with_similarity(card_id)
        if neighbors:
            conn = _db()
            c = conn.cursor()
            for nid, sim in neighbors:
                c.execute("SELECT title, category FROM cards WHERE id=?", (nid,))
                row = c.fetchone()
                label = f"{row[0][:40]} [{row[1]}]" if row else nid
                print(f"  ↔ {label}  (cos={sim:.3f})")
            conn.close()
        else:
            print("  无 link 邻居")
    except Exception as e:
        print(f"link 查询失败: {e}")


def cmd_reslog(args):
    """查看卡片划掉审计日志。args: --last 20 (最近N条)"""
    log_path = os.path.join(PROJECT_ROOT, "memory", "resolution_log.jsonl")
    if not os.path.exists(log_path):
        print("(暂无划卡记录)")
        return
    limit = 20
    if "--last" in args:
        idx = args.index("--last")
        try:
            limit = int(args[idx + 1])
        except (IndexError, ValueError):
            pass
    try:
        with open(log_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        for line in lines[-limit:]:
            entry = json.loads(line.strip())
            print(f"{entry['ts']} | {entry['source']:28s} | {entry['card_id']} | {entry['title']}")
            if entry.get("details"):
                print(f"  → {entry['details']}")
    except Exception as e:
        print(f"读取日志失败: {e}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    if cmd == "cards":
        cmd_cards(rest)
    elif cmd == "chat":
        cmd_chat(rest)
    elif cmd == "diary":
        cmd_diary(rest)
    elif cmd == "status":
        cmd_status()
    elif cmd == "recall":
        cmd_recall(rest)
    elif cmd == "links":
        cmd_links(rest)
    elif cmd == "reslog":
        cmd_reslog(rest)
    elif cmd == "compress":
        print("正在压缩滚动总结...")
        try:
            from delegate.dreaming import ROLLING_SUMMARY_PATH
            from delegate_tools import delegate, atomic_write_text
            if os.path.exists(ROLLING_SUMMARY_PATH):
                with open(ROLLING_SUMMARY_PATH, "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                if raw:
                    compress_prompt = (
                        "你是一个记忆压缩器。下面是主人最近几天的日记式生活叙事，"
                        "请把它压缩成一段 500 字以内的中文滚动总结（一段话，不分点）。"
                        "保留关键事件、重要承诺、情绪变化、关键日期，丢弃流水账。\n\n"
                        f"{raw}"
                    )
                    compressed = delegate(compress_prompt, "")
                    if compressed and 20 < len(compressed) < 800:
                        atomic_write_text(ROLLING_SUMMARY_PATH, compressed.strip() + "\n")
                        print(f"压缩完成 ({len(compressed)} 字)")
                    else:
                        print("压缩结果异常，保留原文件")
                else:
                    print("rolling_summary.md 为空")
            else:
                print("rolling_summary.md 不存在")
        except Exception as e:
            print(f"压缩失败: {e}")
    else:
        print(f"未知命令: {cmd}")
        print(__doc__)
