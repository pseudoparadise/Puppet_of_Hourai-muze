"""
preflight.py — Claude Code 家模式每轮消息的 ghost-trigger 管线
用法:
  python preflight.py "用户消息"
  python preflight.py --json "用户消息"    纯 JSON 输出（给 Claude Code 解析）
  python preflight.py --skip-va "用户消息"  跳过 VA 估测（省钱，固定 mid 档）
  echo "用户消息" | python preflight.py     管道输入

做的事:
  VA 估测 → 记忆检索 → 渐进退化过滤 → 输出卡片上下文 + 管线反馈

渐进退化规则（模拟人类遗忘曲线）:
  第1次     → 完整：标题+原话+概括（首次曝光）
  第N-M次   → 仅标题（知道有这回事，想不起细节）
  第P-Q次   → 标题+原话（遗忘曲线最陡峭时，关键句闪回）
  第R-S次   → 静默（深度冻结）
  S+次      → 余弦门禁：query vs card embedding cos > threshold → 放完整
              否则仅标题

分类差异化:
  deep_talks/milestone/turning_points → 标题2-4, 原话5-7, 静默8-11, reopen@12, threshold 0.75
  preferences/habits                  → 标题2-3, 原话4-5, 静默6-8,  reopen@9,  threshold 0.70
  其他                                → 标题2-3, 原话4-4, 静默5-6,  reopen@7,  threshold 0.65
"""
import json
import os
import sys
import sqlite3
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "memory"))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "emotion"))

from memory.retriever import retrieve, get_va_tier
from crash_reporter import crash_print

SEEN_PATH = os.path.join(PROJECT_ROOT, "memory", "preflight_seen.json")
DB_PATH = os.path.join(PROJECT_ROOT, "memory", "cards.db")

DEGRADATION = {
    "deep_talks":     {"title": (2, 4), "quote": (5, 7), "silent": (8, 11), "reopen_at": 12, "threshold": 0.75},
    "milestone":      {"title": (2, 4), "quote": (5, 7), "silent": (8, 11), "reopen_at": 12, "threshold": 0.75},
    "turning_points": {"title": (2, 4), "quote": (5, 7), "silent": (8, 11), "reopen_at": 12, "threshold": 0.75},
    "preferences":    {"title": (2, 3), "quote": (4, 5), "silent": (6, 8),  "reopen_at": 9,  "threshold": 0.70},
    "habits":         {"title": (2, 3), "quote": (4, 5), "silent": (6, 8),  "reopen_at": 9,  "threshold": 0.70},
    "_default":       {"title": (2, 3), "quote": (4, 4), "silent": (5, 6),  "reopen_at": 7,  "threshold": 0.65},
}


def _load_seen() -> dict:
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            crash_print(e, "preflight 加载 seen.json")
    return {"round": 0, "cards": {}}


def _save_seen(data: dict):
    try:
        os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
        from delegate_tools import atomic_write_json
        atomic_write_json(SEEN_PATH, data)
    except Exception as e:
        crash_print(e, "preflight 写入 seen.json (atomic)")
        try:
            with open(SEEN_PATH, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e2:
            crash_print(e2, "preflight 备援写入 seen.json")


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    dot = np.dot(a, b)
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    return float(dot / (na * nb)) if na * nb > 0 else 0.0


def _extract_quote(content: str) -> str:
    """从卡片正文提取「原话」部分。匹配 '原话：... | 概括：' 或 '原话：...概括：'。"""
    if not content:
        return ""
    import re
    m = re.search(r'原话[：:](.*?)(?:\s*[|‖]\s*概括[：:]|\n概括[：:]|\Z)', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return content[:200]


def _extract_summary(content: str) -> str:
    """从卡片正文提取「概括」部分。匹配 '概括：...' 到文末。"""
    if not content:
        return ""
    import re
    m = re.search(r'(?:\s*[|‖]\s*概括[：:]|\n概括[：:])(.*?)\Z', content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return ""


def _get_card_embeddings(card_ids: list) -> dict:
    """批量读取卡片 embedding，返回 {card_id: np.ndarray}。一次 SQL 连接。"""
    result = {}
    if not card_ids:
        return result
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        placeholders = ",".join("?" * len(card_ids))
        c.execute(f"SELECT id, embedding FROM cards WHERE id IN ({placeholders})", card_ids)
        for row in c.fetchall():
            if row["embedding"]:
                result[row["id"]] = np.frombuffer(row["embedding"], dtype=np.float32)
        conn.close()
    except Exception as e:
        crash_print(e, "preflight 批量读取卡片 embedding")
    return result


def _relevance_tier(cos_map: dict) -> dict:
    """按 cos 值相对排名分配 high/medium/low 档。cos_map: {card_id: cos_value}"""
    if not cos_map:
        return {}
    sorted_items = sorted(cos_map.items(), key=lambda x: x[1], reverse=True)
    n = len(sorted_items)
    tiers = {}
    for rank, (cid, cos) in enumerate(sorted_items):
        if n == 1:
            tiers[cid] = "high"
        elif n == 2:
            tiers[cid] = "high" if rank == 0 else "low"
        else:
            if rank == 0:
                tiers[cid] = "high"
            elif rank == n - 1:
                tiers[cid] = "low"
            else:
                tiers[cid] = "medium"
    return tiers


def _card_display(card_id: str, category: str) -> str:
    """返回 display 等级: full / title / quote / silent / reopen"""
    rule = DEGRADATION.get(category, DEGRADATION["_default"])
    seen = _load_seen()
    card_entry = seen["cards"].get(card_id, {"times_shown": 0})
    times = card_entry.get("times_shown", 0) + 1

    if times == 1:
        return "full", times

    t_low, t_high = rule["title"]
    if t_low <= times <= t_high:
        return "title", times

    q_low, q_high = rule["quote"]
    if q_low <= times <= q_high:
        return "quote", times

    s_low, s_high = rule["silent"]
    if s_low <= times <= s_high:
        return "silent", times

    if times >= rule["reopen_at"]:
        return "reopen", times

    return "full", times


def _check_reopen(card_id: str, threshold: float) -> bool:
    """余弦门禁：query_vec vs card embedding。True=放行正文。"""
    try:
        qv = getattr(retrieve, '_cached_query_vec', None)
        if qv is None:
            return False

        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT embedding FROM cards WHERE id=?", (card_id,))
        row = c.fetchone()
        conn.close()

        if not row or not row["embedding"]:
            return False

        cv = np.frombuffer(row["embedding"], dtype=np.float32)
        return _cosine(qv, cv) > threshold
    except Exception as e:
        crash_print(e, "preflight reopen 余弦门禁")
        return False


def _parse_ts(ts_str: str):
    """统一解析 ISO timestamp，兼容 Z / +00:00 / +0000 三种格式。"""
    try:
        from datetime import datetime
        s = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except Exception:
        return None


def _backfill_ghost_reply():
    """检测 chat_logs.json 中是否存在 orphan user（上轮 ghost 未写入），
    从 Claude Code session 文件中捞取 ghost 回复并补写。循环修复直到无连续 user。"""
    chat_path = os.path.join(PROJECT_ROOT, "chat_logs.json")
    if not os.path.exists(chat_path):
        return

    # 预扫 session 文件列表（一次）
    # 从 session 注册文件读取所有 Claude Code 窗口，不限于 ghost-trigger 目录
    sessions_dir = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    projects_base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    session_files = []
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
            # 从 cwd 推导项目目录名
            proj_name = "C--" + cwd.replace(":", "").replace("\\", "-").replace("/", "-").strip("-")
            candidate = os.path.join(projects_base, proj_name, f"{sid}.jsonl")
            if os.path.exists(candidate):
                session_files.append((os.path.getmtime(candidate), candidate))
    except Exception:
        pass
    if not session_files:
        return
    session_files.sort(key=lambda x: x[0], reverse=True)

    fixed_any = False
    max_loops = 5  # 安全阀

    for _ in range(max_loops):
        entries = []
        try:
            with open(chat_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        entries.append(json.loads(line))
        except Exception:
            return

        if len(entries) < 2:
            return

        last = entries[-1]
        prev = entries[-2]

        if not (last.get("role") == "user" and prev.get("role") == "user"):
            return  # 无 orphan，退出循环

        t1 = _parse_ts(prev.get("timestamp", ""))
        t2 = _parse_ts(last.get("timestamp", ""))
        if not t1 or not t2:
            return

        ghost_content = None
        ghost_ts = None

        for _, spath in session_files:
            try:
                with open(spath, "r", encoding="utf-8") as sf:
                    for line in sf:
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
                        content_list = entry.get("message", {}).get("content", [])
                        for block in content_list:
                            if block.get("type") == "text":
                                ghost_content = block.get("text", "")
                                break
                        if ghost_content:
                            ghost_ts = entry.get("timestamp", "")
                            break
            except Exception:
                pass
            if ghost_content:
                break

        if not ghost_content:
            return  # 找不到，放弃本轮及后续

        # 去重
        dup = False
        for e in entries:
            if e.get("role") == "ghost":
                if e.get("timestamp", "") == ghost_ts:
                    dup = True
                    break
                if e.get("content", "") == ghost_content:
                    dup = True
                    break
        if dup:
            return

        # 补写
        ghost_entry = {"timestamp": ghost_ts, "role": "ghost", "content": ghost_content}
        try:
            with open(chat_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ghost_entry, ensure_ascii=False) + "\n")
            print(f"[preflight] 兜底补写 ghost 回复 → chat_logs.json ({ghost_ts[:19]})")
            fixed_any = True
        except Exception as e:
            crash_print(e, "preflight 兜底补写 ghost 回复")
            return


def preflight(user_input: str, json_mode: bool = False, skip_va: bool = False) -> dict:
    # ── 兜底：补写上轮遗漏的 ghost 回复（仅家模式） ──
    from shared import get_mode
    if get_mode() == "home":
        _backfill_ghost_reply()

    seen = _load_seen()
    seen["round"] = seen.get("round", 0) + 1
    current_round = seen["round"]

    va = None
    va_tier = "mid"
    va_info = {}
    mva_path = os.path.join(PROJECT_ROOT, "manual_va.json")
    mva_cc_override = False

    if os.path.exists(mva_path):
        try:
            with open(mva_path, "r", encoding="utf-8") as f:
                mva = json.load(f)
            mva_cc_override = mva.get("claude_va_override", False)
        except Exception as e:
            crash_print(e, "preflight 加载 manual_va.json")

    if mva_cc_override:
        mva_enabled = mva.get("enabled", False)
        if mva_enabled:
            va = {"description": "console手动设定(CC)", "valence": mva.get("valence", 0.0),
                  "arousal": mva.get("arousal", 0.5)}
            va_tier = get_va_tier(va["arousal"])
            va_info = {
                "valence": round(va["valence"], 3),
                "arousal": round(va["arousal"], 3),
                "tier": va_tier,
                "description": va["description"],
                "source": "console_manual",
            }
        else:
            va = {"description": "", "valence": 0.0, "arousal": 0.5}
            va_info = {"valence": 0.0, "arousal": 0.5, "tier": "mid", "description": "",
                        "source": "console_override(no_manual_data)"}
    elif not skip_va:
        from emotion.va_estimator import estimate as va_estimate
        try:
            va = va_estimate(user_input)
            va_tier = get_va_tier(va["arousal"])
            va_info = {
                "valence": round(va["valence"], 3),
                "arousal": round(va["arousal"], 3),
                "tier": va_tier,
                "description": va.get("description", ""),
                "source": "deepseek",
            }
        except Exception as e:
            crash_print(e, "preflight VA 估测")
            va_info = {"valence": 0.0, "arousal": 0.5, "tier": "mid", "description": "",
                        "error": str(e)}
            va = {"description": "", "valence": 0.0, "arousal": 0.5}
    else:
        va = {"description": "", "valence": 0.0, "arousal": 0.5}
        va_info = {"valence": 0.0, "arousal": 0.5, "tier": "mid", "description": "",
                    "source": "skip-va"}

    top_cards = []
    va_description = va.get("description", "") if va else ""
    try:
        top_cards = retrieve(
            user_input, top_k=3,
            va_tier=va_tier,
            va_description=va_description,
            va_valence=va.get("valence") if va else None,
            va_arousal=va.get("arousal") if va else None,
            trace_tag="preflight",
        )
        try:
            from memory.memory_manager import touch_cards
            touch_cards([c["id"] for c in top_cards])
        except Exception as e:
            crash_print(e, "preflight 加载 manual_va.json")
    except Exception as e:
        crash_print(e, "preflight 记忆检索")
        top_cards = [{"error": str(e), "title": "检索失败"}]

    # ── 语义相关性门控：计算每张卡 vs query 的余弦相似度 ──
    qv = getattr(retrieve, '_cached_query_vec', None)
    card_ids = [c.get("id", "") for c in top_cards]
    embeddings = _get_card_embeddings(card_ids) if qv is not None else {}
    cos_map = {}  # card_id → cos
    if qv is not None:
        for cid, emb in embeddings.items():
            cos_map[cid] = _cosine(qv, emb)
    relevance_tiers = _relevance_tier(cos_map)

    output_cards = []
    silent_count = 0

    for c in top_cards:
        card_id = c.get("id", "")
        category = c.get("category", "")
        content_full = c.get("content", "")
        keywords = c.get("keywords", "")

        # A. 渐进退化层级
        deg_display, new_times = _card_display(card_id, category)

        if deg_display == "silent":
            _update_seen_entry(seen, card_id, new_times)
            silent_count += 1
            continue

        # B. 语义相关性层级
        rel_tier = relevance_tiers.get(card_id, "medium")
        rel_cos = cos_map.get(card_id, 0.0)
        # 相关性想要的展示级别
        if rel_tier == "high":
            rel_wants = "full"
        elif rel_tier == "medium":
            rel_wants = "summary"
        else:
            rel_wants = "title"

        # C. 双层门控组合
        # 展示级别阶梯: full > summary > quote > title
        DISPLAY_ORDER = {"full": 4, "summary": 3, "quote": 2, "title": 1}

        if deg_display == "reopen":
            rule = DEGRADATION.get(category, DEGRADATION["_default"])
            if _check_reopen(card_id, rule["threshold"]):
                deg_display = "full"
            else:
                deg_display = "title"

        deg_level = DISPLAY_ORDER.get(deg_display, 1)
        rel_level = DISPLAY_ORDER.get(rel_wants, 1)

        # 静默不可破（已在上面 continue），剩下：相关高升舱，相关低节流
        if rel_tier == "high" and deg_level < 4:
            final_display = "full"  # 语义高度匹配 → 破例升到全文
        elif rel_tier == "low" and deg_level >= 4:
            final_display = "summary"  # 退化全量但语义低 → 降为概括
        elif rel_tier == "low" and deg_level >= 3:
            final_display = "title"  # 退化不够+语义低 → 仅标题
        else:
            # 取两者中较高的（但不超过 deg 的上限）
            final_level = min(deg_level, rel_level) if rel_tier == "low" else max(deg_level, rel_level)
            final_display = {v: k for k, v in DISPLAY_ORDER.items()}.get(final_level, deg_display)

        # D. 组装输出
        card_out = {
            "id": card_id,
            "title": c.get("title", ""),
            "category": category,
            "score": round(c.get("score", 0), 1),
            "importance": c.get("importance", 0),
            "hit_count": c.get("hit_count", 0),
            "relevance": rel_tier,
            "cos": round(rel_cos, 4),
        }

        if final_display == "full":
            card_out["content"] = content_full
            card_out["display"] = "full"
        elif final_display == "summary":
            card_out["content"] = _extract_summary(content_full)
            card_out["display"] = "summary"
        elif final_display == "quote":
            card_out["content"] = _extract_quote(content_full)
            card_out["display"] = "quote"
        else:
            card_out["content"] = ""
            card_out["keywords"] = keywords
            card_out["display"] = "title"

        _update_seen_entry(seen, card_id, new_times)
        output_cards.append(card_out)

    _save_seen(seen)

    result = {"va": va_info, "cards": output_cards}
    if silent_count > 0:
        result["silent_count"] = silent_count

    if json_mode:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f"┌{' VA ' + '─' * 51}┐")
        print(f"│ 情绪: v={va_info['valence']:.2f} a={va_info['arousal']:.2f}  tier={va_info['tier']:<5}                │")
        if va_info.get("description"):
            desc_short = va_info["description"][:42]
            print(f"│ 描述: {desc_short:<46} │")
        print(f"├{' 记忆检索 ' + '─' * 47}┤")
        if output_cards:
            for card in output_cards:
                title = card.get("title", "")[:35]
                cat = card.get("category", "?")[:12]
                score = card.get("score", 0)
                imp = card.get("importance", 0)
                d = card.get("display", "full")
                r = card.get("relevance", "")
                display_marker = {"title": "◁标题", "quote": "◁原话", "summary": "◁概括", "full": "◁全文"}.get(d, "")
                rel_marker = {"high": " ★", "medium": "", "low": " ·"}.get(r, "")
                print(f"│ [{cat:<12}] {title:<30} s={score:4.1f} imp={imp:>2} {display_marker}{rel_marker} │")
        else:
            print(f"│ (无相关记忆)                                               │")
        if silent_count > 0:
            print(f"│ ({silent_count} 张卡在深度冷却中)                                      │")
        print(f"└{'─' * 55}┘")

    return result


def _update_seen_entry(seen: dict, card_id: str, times: int):
    if card_id not in seen.setdefault("cards", {}):
        seen["cards"][card_id] = {"times_shown": 0, "first_seen_round": seen["round"]}
    seen["cards"][card_id]["times_shown"] = times
    seen["cards"][card_id]["last_shown_round"] = seen["round"]


def _reset_degradation_counter(card_id: str):
    """复权卡片时清空退化轮数，让卡片从 FULL(首次曝光) 重新开始。"""
    seen = _load_seen()
    if card_id in seen.get("cards", {}):
        del seen["cards"][card_id]
        _save_seen(seen)
        print(f"[preflight] 退化轮数已清零: {card_id}")
    else:
        print(f"[preflight] 卡片 {card_id} 不在退化记录中，无需清零。")


if __name__ == "__main__":
    args = sys.argv[1:]
    json_mode = "--json" in args
    skip_va = "--skip-va" in args
    input_args = [a for a in args if a not in ("--json", "--skip-va")]
    user_input = " ".join(input_args).strip()

    if not user_input:
        if not sys.stdin.isatty():
            user_input = sys.stdin.read().strip()
        if not user_input:
            print("用法: python preflight.py [--json] \"用户消息\"")
            sys.exit(1)

    preflight(user_input, json_mode=json_mode, skip_va=skip_va)
