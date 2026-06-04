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
from clock import beijing_now, beijing_today, beijing_yesterday

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
PENDING_CARDS_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "pending_cards.json")
ROLLING_SUMMARY_PATH = os.path.join(os.path.dirname(__file__), "..", "memory", "rolling_summary.md")

def _load_prompt(name):
    path = os.path.join(PROMPTS_DIR, name)
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()

# ═══════════════════════════════════════════════════════════════
#  日期工具：统一使用 clock.py，日记永远写「昨天」
# ═══════════════════════════════════════════════════════════════

def _parse_utc_ts(ts_str: str) -> datetime | None:
    """解析 chat_logs 中的 UTC 时间戳为 aware datetime。"""
    try:
        return datetime.fromisoformat(ts_str.replace('+0000', '+00:00'))
    except Exception:
        return None

def _get_digest_for_date(target_date: str):
    """
    读取 chat_logs.json + chat_logs_work.json + work_log，
    筛选北京时间 target_date 当天的全部内容。
    返回 (digest_text | None, target_date)。
    """
    from datetime import timezone as _tz
    base = os.path.dirname(__file__)
    lines = []

    # 1. 家模式 chat_logs.json
    for log_file in ["chat_logs.json", "chat_logs_work.json"]:
        log_path = os.path.join(base, "..", log_file)
        if not os.path.exists(log_path):
            continue
        with open(log_path, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    ts_str = entry.get("timestamp", "")
                    dt = _parse_utc_ts(ts_str)
                    if dt is None: continue
                    bj_dt = dt.astimezone(_tz(timedelta(hours=8)))
                    if bj_dt.strftime("%Y-%m-%d") != target_date: continue
                    role = "我" if entry.get("role") == "ghost" else "她"
                    prefix = "[工位]" if log_file == "chat_logs_work.json" else ""
                    lines.append(f"{prefix}{role}: {entry.get('content', '')}")
                except Exception:
                    pass

    # 2. work_log 工位任务记录
    work_path = os.path.join(base, "..", "diary", "work", f"{target_date}_work.md")
    if os.path.exists(work_path):
        lines.append("--- 今日工位任务 ---")
        with open(work_path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)

    if not lines:
        return None, target_date
    return "\n".join(lines[-80:]), target_date

def _update_rolling_summary(new_summary: str, date_str: str = None):
    """追加/更新滚动总结，并压缩为一段精炼文本（≤500字）。"""
    if date_str is None:
        date_str = beijing_today()

    # 收集所有已有日期段 + 新的一天
    if os.path.exists(ROLLING_SUMMARY_PATH):
        with open(ROLLING_SUMMARY_PATH, "r", encoding="utf-8") as f:
            old = f.read()
    else:
        old = ""

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

    segments = [s for s in segments if not s.startswith(f"## {date_str}")]
    segments.append(f"## {date_str}\n{new_summary}")
    segments = segments[-7:]

    raw = "\n\n".join(s.strip() for s in segments).strip()

    # ── 压缩：多于 2 天时，调 DeepSeek 精炼为 ≤500 字总结 ──
    if len(segments) >= 2:
        try:
            compress_prompt = (
                "你是一个记忆压缩器。下面是你主人最近几天的日记式生活叙事，"
                "请把它压缩成一段 500 字以内的中文滚动总结（一段话，不分点）。"
                "保留关键事件、重要承诺、情绪变化、关键日期，丢弃流水账。\n\n"
                f"{raw}"
            )
            compressed = delegate(compress_prompt, "")
            if compressed and 20 < len(compressed) < 800:
                raw = f"## {date_str} (压缩)\n{compressed.strip()}"
            else:
                raw = f"## {date_str} (压缩)\n{compressed[:500].strip() if compressed else new_summary[:500]}"
        except Exception as e:
            print(f"[rolling] 压缩失败，保留原始: {e}")

    from delegate_tools import atomic_write_text
    atomic_write_text(ROLLING_SUMMARY_PATH, raw + "\n")

def _append_pending_card(card: dict, source_module: str = "dreaming.py", evidence: str = ""):
    """将卡片草稿写入 pending_cards.json — 强制弹窗审核后方可写入"""
    from shared import is_garbage_card
    reason = is_garbage_card(card.get("title", ""), card.get("content", ""))
    if reason:
        print(f"[dreaming] 拦截: {reason}")
        return

    # ── 强制弹窗审核 ──
    try:
        from card_review_popup import review_card_popup
        evidence_text = evidence or card.get("content", "")[:300]
        reviewed = review_card_popup(dict(card), source_module, evidence_text)
        if reviewed is None:
            print(f"[dreaming] 人类拒绝「{card.get('title','')}」")
            return
        card = reviewed
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise RuntimeError(f"[dreaming审核弹窗失败] title={card.get('title','')}: {e}")

    from delegate_tools import atomic_write_json
    from shared import load_json_safe
    pending = load_json_safe(PENDING_CARDS_PATH, default=[], label="dreaming")
    if '_embed_vec' not in card:
        try:
            from encoder import embed as _embed_dream
            _dv = _embed_dream(card.get('title', '') + ' ' + card.get('content', ''))
            card['_embed_vec'] = _dv.tolist()
        except Exception:
            pass
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

def chain_dream(target_date: str = None):
    """生成日记 + 总结 + 立卡。target_date 为北京日期，默认昨天。"""
    result = {"step1": None, "step2": None, "step3": None}

    if target_date is None:
        target_date = beijing_yesterday()

    digest_result = _get_digest_for_date(target_date)
    digest = digest_result[0] if digest_result else None
    digest_date = digest_result[1] if digest_result else target_date

    diary_dir = os.path.join(os.path.dirname(__file__), "..", "diary")
    os.makedirs(diary_dir, exist_ok=True)
    diary_path = os.path.join(diary_dir, f"{digest_date}.md")

    diary_prompt = _load_prompt("dreaming_diary.txt")
    diary_json = None
    summary = ""

    if not digest:
        # ── 降级保护：无聊天日也生成空日记 + 空 events.json，防止管道断裂 ──
        print(f"[dreaming] {digest_date} 无对话记录，生成空日记降级。")
        from delegate_tools import atomic_write_text
        quiet_md = f"# {digest_date}\n\n## 日记\n今天是安静的一天，没有和 DS 的对话记录。\n"
        atomic_write_text(diary_path, quiet_md)
        print(f"[dreaming] 空日记已落盘: {diary_path}")

        quiet_events = {
            "narrative_short": "安静的一天，无对话。",
            "completions": [],
            "cards_created": [],
            "eisenhower": {},
            "calendar": [],
        }
        events_path = os.path.join(diary_dir, f"{digest_date}_events.json")
        try:
            from delegate_tools import atomic_write_json
            atomic_write_json(events_path, quiet_events)
            print(f"[dreaming] 空事件日志已落盘: {events_path}")
        except Exception as e:
            print(f"[dreaming] 空事件日志写入失败: {e}")
        result["step1"] = quiet_events
        result["step2.5"] = quiet_events
        summary = f"{digest_date}：安静的一天，无对话。"
        result["step2"] = summary
        _update_rolling_summary(summary, digest_date)
        return result

    step1_context = f"今日对话摘要：\n{digest}\n当前日期：{digest_date}"
    diary_raw = delegate(diary_prompt, step1_context)
    print(f"[dreaming] Step1 日记: {diary_raw[:100]}...")

    # 解析 AI 输出的 JSON 日记格式
    from shared import llm_to_json
    diary_json = llm_to_json(diary_raw)

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
    _update_rolling_summary(summary, digest_date)

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
    from shared import llm_to_json
    card_json = llm_to_json(card_raw, default={"action": "skip"})

    if card_json.get("action") == "create":
        from clock import beijing_now as _now4
        # ── VA 估算：对今日对话做情绪估值，替代硬编码默认值 ──
        try:
            from emotion.va_estimator import estimate as va_estimate
            va_result = va_estimate(digest)
            chord_val = va_result.get("chord", "")
            valence_val = va_result.get("valence", 0.0)
            arousal_val = va_result.get("arousal", 0.5)
        except Exception:
            chord_val, valence_val, arousal_val = "", 0.0, 0.5
        safe_title = card_json.get("title", "auto").replace(" ", "_").replace("/", "_")[:60]
        cat = card_json.get("category", "interaction")
        draft_type = "event" if cat in ("milestone", "turning_points", "commitments") else "fact"
        card_draft = {
            "id": f"{_now4().strftime('%Y%m%d')}_{safe_title}",
            "title": card_json.get("title", ""),
            "content": card_json.get("content", ""),
            "keywords": card_json.get("keywords", ""),
            "importance": max(card_json.get("importance", 5), 8) if cat in {'deep_talks', 'milestone', 'turning_points'} else card_json.get("importance", 5),
            "category": cat,
            "type": card_json.get("type", draft_type),
            "proposed_by": "dreaming",
            "proposed_at": _now4().isoformat(),
            "review_status": "pending",
            "chord": chord_val,
            "valence": valence_val,
            "arousal": arousal_val
        }
        dream_evidence = f"今日总结：\n{summary}\n\n完整对话摘要：\n{digest}"
        _append_pending_card(card_draft, "dreaming.py（做梦链）", dream_evidence[:500])
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
    from delegate_tools import atomic_write_text

    today = beijing_today()
    diary_dir = os.path.join(os.path.dirname(__file__), "..", "diary")
    weekly_path = os.path.join(diary_dir, f"weekly_{today}.md")

    # 收集近7天事件（完成 + 日历，待办从 cards.db 取）
    all_completions = []
    all_calendar = []

    from datetime import timedelta as _td_s
    for days_back in range(7, 0, -1):
        d = (beijing_now() - _td_s(days=days_back)).strftime("%Y-%m-%d")
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

    # ── 待办：以 cards.db 中 resolved=0 的卡片为准，不对齐过时的 events.json ──
    db_todos = {"重要且紧急": [], "重要不紧急": [], "不重要但紧急": [], "不重要不紧急": []}
    try:
        import sqlite3 as _sql_todo
        _tdb = _sql_todo.connect(os.path.join(os.path.dirname(__file__), "..", "memory", "cards.db"))
        _tdb.row_factory = _sql_todo.Row
        _trows = _tdb.execute("""
            SELECT id, title, category, importance, target_date
            FROM cards
            WHERE review_status='final' AND resolved=0
              AND category IN ('todo', 'commitments', 'daily_life')
            ORDER BY importance DESC
        """).fetchall()
        _tdb.close()
        for tr in _trows:
            imp = tr["importance"]
            td = tr["target_date"] or ""
            # 艾森豪威尔分类（与 get_todo_list 一致）
            if imp >= 8:
                quad = "重要不紧急"
            elif td and td < today:
                quad = "重要且紧急"
            elif tr["category"] == 'todo':
                try:
                    from datetime import datetime as _dt_t
                    days_left = (_dt_t.strptime(td, '%Y-%m-%d') - datetime.now()).days if td else 999
                    quad = "重要且紧急" if days_left <= 7 else "重要不紧急"
                except:
                    quad = "重要不紧急"
            elif tr["category"] == 'commitments':
                quad = "重要不紧急" if imp >= 7 else "不重要但紧急"
            else:
                quad = "不重要不紧急"
            db_todos.setdefault(quad, []).append((tr["title"], td))
    except Exception as e:
        print(f"[weekly_sweep] DB 待办读取失败: {e}")

    for label in ("重要且紧急", "重要不紧急", "不重要但紧急", "不重要不紧急"):
        items = db_todos.get(label, [])
        if items:
            md += f"## {label}\n"
            for title, td in items:
                dl = f" 📅{td}" if td else ""
                md += f"- {title}{dl}\n"
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
    from crash_reporter import install
    install()
    chain_dream()
    print("OK - dreaming.py 就绪")