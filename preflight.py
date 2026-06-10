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
import time
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


def _get_card_embeddings_triple(card_ids: list) -> dict:
    """批量读取三向量 embedding，返回 {card_id: {'summary': ndarray, 'kw': ndarray, 'quote': ndarray}}。"""
    result = {}
    if not card_ids:
        return result
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        placeholders = ",".join("?" * len(card_ids))
        c.execute(
            f"SELECT id, embedding, embedding_kw, embedding_quote FROM cards WHERE id IN ({placeholders})",
            card_ids
        )
        for row in c.fetchall():
            entry = {}
            if row["embedding"]:
                entry["summary"] = np.frombuffer(row["embedding"], dtype=np.float32)
            if row["embedding_kw"]:
                entry["kw"] = np.frombuffer(row["embedding_kw"], dtype=np.float32)
            if row["embedding_quote"]:
                entry["quote"] = np.frombuffer(row["embedding_quote"], dtype=np.float32)
            if entry:
                result[row["id"]] = entry
        conn.close()
    except Exception as e:
        crash_print(e, "preflight 批量读取三向量 embedding")
    return result


def _get_card_embeddings(card_ids: list) -> dict:
    """[兼容] 返回摘要向量 {card_id: ndarray}，供旧代码使用。"""
    triple = _get_card_embeddings_triple(card_ids)
    return {cid: embs["summary"] for cid, embs in triple.items() if "summary" in embs}


def _triple_cosine_gate(card_id: str, deg_display: str, query_vec: np.ndarray,
                          triple_embs: dict, category: str) -> str:
    """
    三权分立门控：根据退化阶段，用对应的向量做余弦比对，决定是否升级展示级别。

    阶段 → 比对方式:
      TITLE   → query vs embedding_kw, cos>0.50 → 升级为 summary
      QUOTE   → query vs embedding_quote, cos>0.60 → 升级为 full
      SUMMARY → query vs embedding (summary), cos>0.50 → 升级为 full
      REOPEN  → 三路 max > threshold → full; 否则 title（比旧版单向量更宽容）
      FULL    → 不比对，直接放行

    返回: 可能升级后的 display 等级。
    """
    embs = triple_embs.get(card_id, {})
    if not embs or query_vec is None:
        return deg_display

    rule = DEGRADATION.get(category, DEGRADATION["_default"])

    if deg_display == "title":
        # 关键词向量比对：query 和关键词语义接近 → 至少给概括
        if "kw" in embs:
            cos_kw = _cosine(query_vec, embs["kw"])
            if cos_kw > 0.50:
                return "summary"

    elif deg_display == "quote":
        # 原话向量比对：query 和原话语义接近 → 直接给全文
        if "quote" in embs:
            cos_quote = _cosine(query_vec, embs["quote"])
            if cos_quote > 0.60:
                return "full"

    elif deg_display == "summary":
        # 摘要向量比对：query 和概括高度相关 → 升级全文
        if "summary" in embs:
            cos_summary = _cosine(query_vec, embs["summary"])
            if cos_summary > 0.50:
                return "full"

    elif deg_display == "reopen":
        # 三路任一过线即放行
        cos_list = []
        for key in ("kw", "quote", "summary"):
            if key in embs:
                cos_list.append(_cosine(query_vec, embs[key]))
        max_cos = max(cos_list) if cos_list else 0.0
        if max_cos > rule["threshold"]:
            return "full"
        else:
            return "title"

    return deg_display


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
    """统一解析 ISO timestamp 为 timezone-aware datetime。
    兼容 Z / +00:00 / +0000 / 无时区 四种格式。无时区时默认 UTC。"""
    try:
        from datetime import datetime, timezone
        s = ts_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _backfill_ghost_reply():
    """检测 chat_logs.json 中是否存在 orphan user（上轮 ghost 未写入），
    从 Claude Code session 文件中捞取 ghost 回复并补写。

    一个对话框可以产生多个 session 文件。按 orphan 时间戳匹配 session mtime：
    session 的 mtime 在 user1 之后 → 该 session 可能包含对应 ghost 回复。
    只读 session 末尾 3000 行（orphan 回复总是在末尾附近）。"""
    import time as _time_bf

    chat_path = os.path.join(PROJECT_ROOT, "chat_logs.json")
    if not os.path.exists(chat_path):
        return

    # 收集所有 ghost-trigger 项目的 session 文件（按 mtime 降序）
    sessions_dir = os.path.join(os.path.expanduser("~"), ".claude", "sessions")
    projects_base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    ghost_sessions = []  # [(mtime, filepath), ...]
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
            if "ghost-trigger" in proj_name:
                ghost_sessions.append((os.path.getmtime(candidate), candidate))
    except Exception:
        pass

    if not ghost_sessions:
        return
    ghost_sessions.sort(key=lambda x: x[0], reverse=True)

    _bf_start = _time_bf.time()

    for _ in range(5):
        if _time_bf.time() - _bf_start > 10:
            return

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

        # 从末尾反向扫描找最近一个 orphan 对（两个连续 user）
        orphan_idx = -1
        for i in range(len(entries) - 1, 0, -1):
            if entries[i]["role"] == "user" and entries[i-1]["role"] == "user":
                orphan_idx = i
                break
        if orphan_idx < 0:
            return

        prev = entries[orphan_idx - 1]
        last = entries[orphan_idx]

        t1 = _parse_ts(prev.get("timestamp", ""))
        t2 = _parse_ts(last.get("timestamp", ""))
        if not t1 or not t2:
            return

        # t1 转 unix timestamp 用于和 session mtime 比较
        t1_unix = t1.timestamp()

        ghost_content = None
        ghost_ts = None

        def _scan_lines(lines, t1, t2):
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
                content_list = entry.get("message", {}).get("content", [])
                for block in content_list:
                    if block.get("type") == "text":
                        return block.get("text", ""), entry.get("timestamp", "")
                return None, None
            return None, None

        for session_mtime, spath in ghost_sessions:
            if session_mtime < t1_unix - 3600:
                continue
            # 先扫末尾（快），找不到再扫全文件
            tail = _read_tail(spath, 3000)
            ghost_content, ghost_ts = _scan_lines(tail, t1, t2)
            if not ghost_content:
                # 孤儿太老，回复可能在文件前部 → 全量扫描
                try:
                    with open(spath, "r", encoding="utf-8", errors="replace") as sf:
                        all_lines = sf.readlines()
                    ghost_content, ghost_ts = _scan_lines(all_lines, t1, t2)
                except Exception:
                    pass
            if ghost_content:
                break

        if not ghost_content:
            return

        # 去重检查 + 修正插入位置（如果已存在但位置不对，移到两个 user 之间）
        dup_idx = -1
        for i, e in enumerate(entries):
            if e.get("role") == "ghost" and (
                e.get("timestamp", "") == ghost_ts or e.get("content", "") == ghost_content
            ):
                dup_idx = i
                break

        ghost_entry = {"timestamp": ghost_ts, "role": "ghost", "content": ghost_content}
        if dup_idx >= 0:
            if dup_idx == orphan_idx:
                return  # 已在正确位置，无需操作
            # 已存在但位置不对 → 移到正确位置
            entries.pop(dup_idx)
            if dup_idx < orphan_idx:
                orphan_idx -= 1  # 删除影响插入索引
            entries.insert(orphan_idx, ghost_entry)
        else:
            entries.insert(orphan_idx, ghost_entry)

        try:
            with open(chat_path, "w", encoding="utf-8") as f:
                for e in entries:
                    f.write(json.dumps(e, ensure_ascii=False) + "\n")
            print(f"[preflight] 兜底补写 ghost 回复 → chat_logs.json ({ghost_ts[:19]})")
        except Exception as e:
            crash_print(e, "preflight 兜底补写 ghost 回复")
            return


def _read_tail(filepath: str, max_lines: int) -> list:
    """读文件末尾最多 max_lines 行（大文件友好）。
    对于大文件，chunk 取 max_lines*500 或文件大小的 80%，取较大者。"""
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            fsize = f.tell()
            chunk = max(max_lines * 500, int(fsize * 0.8))
            if fsize <= chunk:
                f.seek(0)
                return f.readlines()
            f.seek(max(0, fsize - chunk))
            f.readline()  # 跳过可能不完整的第一行
            return f.readlines()
    except Exception:
        return []


def preflight(user_input: str, json_mode: bool = False, skip_va: bool = False, music_mode: bool = False) -> dict:
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

    # ── 语义相关性门控 + 三权分立比对 ──
    qv = getattr(retrieve, '_cached_query_vec', None)
    card_ids = [c.get("id", "") for c in top_cards]
    triple_embs = _get_card_embeddings_triple(card_ids) if qv is not None else {}

    # 摘要向量余弦（兼容旧 relevance_tiers 逻辑）
    cos_map = {}
    if qv is not None:
        for cid, embs in triple_embs.items():
            if "summary" in embs:
                cos_map[cid] = _cosine(qv, embs["summary"])
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

        # B. 三权分立门控：根据退化阶段，用对应向量做余弦比对升级
        if qv is not None:
            deg_display = _triple_cosine_gate(card_id, deg_display, qv, triple_embs, category)

        # C. 语义相关性层级
        rel_tier = relevance_tiers.get(card_id, "medium")
        rel_cos = cos_map.get(card_id, 0.0)
        # 相关性想要的展示级别
        if rel_tier == "high":
            rel_wants = "full"
        elif rel_tier == "medium":
            rel_wants = "summary"
        else:
            rel_wants = "title"

        # D. 双层门控组合
        # 展示级别阶梯: full > summary > quote > title
        DISPLAY_ORDER = {"full": 4, "summary": 3, "quote": 2, "title": 1}

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

    # ── 音乐注入：console.py 写入 .music_context.txt，preflight 直接吃现成 ──
    music_ctx_path = os.path.join(PROJECT_ROOT, ".music_context.txt")
    if os.path.exists(music_ctx_path):
        try:
            # 时效检查：超过 5 分钟未更新视为过期
            if time.time() - os.path.getmtime(music_ctx_path) < 300:
                with open(music_ctx_path, "r", encoding="utf-8") as f:
                    music_ctx = f.read().strip()
                if music_ctx:
                    result["music"] = music_ctx
        except Exception:
            pass

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
        if result.get("music"):
            print(f"├{' 音乐 ' + '─' * 51}┤")
            for line in result["music"].split("\n")[:8]:
                print(f"│ {line:<52} │")
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
