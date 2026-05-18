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

    # 毒点5修复：删除所有同日期旧条目，再追加新条目
    segments = [s for s in segments if not s.startswith(f"## {today_str}")]
    segments.append(new_entry.strip())

    # 只保留最近 7 条
    segments = segments[-7:]

    with open(ROLLING_SUMMARY_PATH, "w", encoding="utf-8") as f:
        f.write("\n\n".join(s.strip() for s in segments).strip() + "\n")

def _append_pending_card(card: dict):
    """将卡片草稿写入 pending_cards.json（毒点5修复 — 委托 delegate_tools.atomic_write_json）"""
    from delegate_tools import atomic_write_json
    if os.path.exists(PENDING_CARDS_PATH):
        try:
            with open(PENDING_CARDS_PATH, "r", encoding="utf-8") as f:
                pending = json.load(f)
        except json.JSONDecodeError as e:
            import shutil
            from datetime import datetime
            backup = PENDING_CARDS_PATH + ".corrupted_" + datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(PENDING_CARDS_PATH, backup)
            print(f"[dreaming] ⚠ pending_cards.json 损坏({e.lineno}:{e.colno})，已备份至 {os.path.basename(backup)}，重建空列表")
            pending = []
    else:
        pending = []
    pending.append(card)
    # ── 交叉检查：语义去重 ──
    try:
        from memory.memory_manager import check_duplicates
        dups = check_duplicates(card.get("content", ""))
        if dups:
            print(f"[dreaming] 去重拦截: {dups}")
            return
    except Exception:
        pass

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
    step1_context = f"今日对话摘要：\n{digest}\n当前日期：{digest_date}"
    diary_raw = delegate(diary_prompt, step1_context)
    print(f"[dreaming] Step1 日记: {diary_raw[:100]}...")

    # 解析 AI 输出的 JSON 日记格式
    diary_json = None
    try:
        diary_json = json.loads(diary_raw)
    except:
        import re as _re
        m = _re.search(r'\{.*\}', diary_raw, re.DOTALL)
        if m:
            try:
                diary_json = json.loads(m.group())
            except:
                pass

    if diary_json:
        # 落盘可读 markdown
        narrative = diary_json.get("narrative", "")
        eis = diary_json.get("eisenhower", {})
        completions = diary_json.get("completions_today", [])
        cards_created = diary_json.get("cards_created_today", [])
        calendar_events = diary_json.get("calendar_events", [])

        md = f"# {digest_date}\n\n## 日记\n{narrative}\n\n"
        md += "## 艾森豪威尔四象限\n\n"
        quad_labels = [
            ("重要且紧急", "important_urgent"),
            ("重要不紧急", "important_not_urgent"),
            ("不重要但紧急", "not_important_urgent"),
            ("不重要不紧急", "not_important_not_urgent"),
        ]
        for label, key in quad_labels:
            items = eis.get(key, [])
            if items:
                md += f"### {label}\n"
                for it in items:
                    deadline = f" 📅{it.get('deadline','')}" if it.get('deadline') and it['deadline'] != '无' else ""
                    card = f" [卡:{it.get('card_id','')}]" if it.get('card_id') and it['card_id'] != '无' else ""
                    md += f"- {it.get('item','?')}{deadline}{card} — {it.get('note','')}\n"
                md += "\n"

        if completions:
            md += "## 今日完成\n"
            for c in completions:
                md += f"- ✅ {c}\n"
            md += "\n"

        if calendar_events:
            md += "## 日历事件\n"
            for ce in sorted(calendar_events, key=lambda x: x.get('date', '')):
                md += f"- 📅 {ce.get('date','?')} {ce.get('event','?')}\n"
            md += "\n"

        from delegate_tools import atomic_write_text
        atomic_write_text(diary_path, md.strip() + "\n")
        print(f"[dreaming] 日记已落盘: {diary_path}")
        result["step1"] = diary_json

        # 事件日志（精简版，供 trigger.py 注入 AI 上下文）
        events_json = {
            "narrative_short": narrative[:300],
            "completions": completions,
            "cards_created": cards_created,
            "eisenhower": eis,
            "calendar": calendar_events,
        }
        events_path = os.path.join(diary_dir, f"{digest_date}_events.json")
        try:
            from delegate_tools import atomic_write_json
            atomic_write_json(events_path, events_json)
            print(f"[dreaming] 事件日志已落盘: {events_path}")
        except Exception as e:
            print(f"[dreaming] 事件日志写入失败: {e}")
        result["step2.5"] = events_json

        # 用叙事文本做滚动总结
        summary = narrative[:500]
    else:
        # 降级：AI 没输出 JSON，用原始文本
        from delegate_tools import atomic_write_text
        atomic_write_text(diary_path, f"# {digest_date}\n\n{diary_raw}")
        print(f"[dreaming] 日记（降级纯文本）已落盘: {diary_path}")
        summary = diary_raw[:500]
        result["step1"] = diary_raw

    result["step2"] = summary
    _update_rolling_summary(summary)

    # ── 交叉检查：今日已累积>=3张待审核则跳过立卡 ──
    if os.path.exists(PENDING_CARDS_PATH):
        try:
            with open(PENDING_CARDS_PATH, "r", encoding="utf-8") as pf:
                today_pending = [c for c in json.load(pf)
                    if c.get("proposed_at","").startswith(digest_date)]
            if len(today_pending) >= 3:
                print(f"[dreaming] 今日已累积{len(today_pending)}张待审核，跳过立卡")
                result["step3"] = {"action": "skip", "reason": "pending_queue_full"}
                return result
        except Exception:
            pass

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
            "review_status": "pending",
            "chord": "",
            "valence": 0.0,
            "arousal": 0.5
        }
        _append_pending_card(card_draft)
        result["step3"] = card_draft
        print(f"[dreaming] 卡片草稿已写入 pending_cards.json: {card_draft['id']}")
    else:
        result["step3"] = {"action": "skip"}
        print("[dreaming] 无需立卡，跳过。")

    return result

def weekly_sweep():
    """每7天收拢一次：扫近7天事件日志，合并待办去重，输出 weekly_{date}.md"""
    import glob as _glob
    from datetime import datetime as _dt, timedelta as _td
    from delegate_tools import now_utc, atomic_write_text

    today = now_utc().strftime("%Y-%m-%d")
    diary_dir = os.path.join(os.path.dirname(__file__), "..", "diary")
    weekly_path = os.path.join(diary_dir, f"weekly_{today}.md")

    # 收集近7天事件
    all_completions = []
    all_calendar = []
    eis_merged = {"important_urgent": [], "important_not_urgent": [],
                  "not_important_urgent": [], "not_important_not_urgent": []}

    for days_back in range(7, 0, -1):
        d = (_dt.now() - _td(days=days_back)).strftime("%Y-%m-%d")
        ep = os.path.join(diary_dir, f"{d}_events.json")
        if not os.path.exists(ep):
            continue
        try:
            with open(ep, "r", encoding="utf-8") as f:
                ev = json.load(f)
        except:
            continue
        all_completions.extend(ev.get("completions", []))
        all_calendar.extend(ev.get("calendar", []))
        for key in eis_merged:
            for item in ev.get("eisenhower", {}).get(key, []):
                if item not in eis_merged[key]:
                    eis_merged[key].append(item)

    # 去重日历
    seen_dates = set()
    unique_cal = []
    for ce in sorted(all_calendar, key=lambda x: x.get('date', '')):
        key = ce.get('date', '') + ce.get('event', '')
        if key not in seen_dates:
            unique_cal.append(ce)
            seen_dates.add(key)

    # 写 markdown
    md = f"# 周收拢 — {today}\n\n"
    md += f"覆盖: {today} 前 7 天\n\n"

    quad_labels = [
        ("重要且紧急", "important_urgent"),
        ("重要不紧急", "important_not_urgent"),
        ("不重要但紧急", "not_important_urgent"),
        ("不重要不紧急", "not_important_not_urgent"),
    ]
    for label, key in quad_labels:
        items = eis_merged.get(key, [])
        if items:
            md += f"## {label}\n"
            for it in items:
                dl = f" 📅{it.get('deadline','')}" if it.get('deadline') and it['deadline'] != '无' else ""
                md += f"- {it.get('item','?')}{dl}\n"
            md += "\n"

    if all_completions:
        md += "## 本周完成\n"
        for c in all_completions[:20]:
            md += f"- ✅ {c}\n"
        md += "\n"

    if unique_cal:
        md += "## 即将到来\n"
        from datetime import datetime as _dt2
        now_date = _dt2.now().strftime("%Y-%m-%d")
        upcoming = [ce for ce in unique_cal if ce.get('date', '') >= now_date]
        for ce in sorted(upcoming, key=lambda x: x.get('date', ''))[:10]:
            md += f"- 📅 {ce.get('date','?')} {ce.get('event','?')}\n"
        md += "\n"

    atomic_write_text(weekly_path, md.strip() + "\n")
    print(f"[weekly_sweep] 周收拢已落盘: {weekly_path}")
    return weekly_path


if __name__ == "__main__":
    chain_dream()
    print("OK - dreaming.py 就绪")