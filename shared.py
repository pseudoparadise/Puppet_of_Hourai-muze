"""
shared.py — 全局共享工具模块（DRY 重构）
所有重复的加载/解析/配置逻辑统一出口。
"""
import json, os, re, shutil
from datetime import datetime

# ── 项目根目录（shared.py 本身在根目录） ──
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# ═══════════════════════════════════════════════════════════════
# 1. JSON 安全加载（损坏时备份 + 返回默认值）
# ═══════════════════════════════════════════════════════════════

def load_json_safe(path: str, default=None, label: str = ""):
    """加载 JSON 文件。损坏时备份并返回 default。"""
    if default is None:
        default = []
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        backup = f"{path}.corrupted_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        shutil.copy2(path, backup)
        tag = f"[{label}] " if label else ""
        print(f"{tag}⚠ {os.path.basename(path)} 损坏({e.lineno}:{e.colno})，已备份至 {os.path.basename(backup)}，重建空列表")
        return default


# ═══════════════════════════════════════════════════════════════
# 2. LLM JSON 提取（json.loads 失败时用正则兜底）
# ═══════════════════════════════════════════════════════════════

def llm_to_json(raw: str, default=None):
    """
    从 LLM 输出中提取 JSON。先 json.loads，失败时正则匹配 {…}。
    raw 为空或全部失败时返回 default。
    """
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except Exception:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return default


# ═══════════════════════════════════════════════════════════════
# 3. 中文停用字符集（单例）
# ═══════════════════════════════════════════════════════════════

_ZH_STOP_CHARS = None

def zh_stop_chars() -> set:
    """中文特征提取停用字符集（全局单例）。"""
    global _ZH_STOP_CHARS
    if _ZH_STOP_CHARS is None:
        _ZH_STOP_CHARS = set(
            '的了是在我有他个这着就和也要可会你他们来到说去为上对得大子能过下一地出道自以时年看没那天家开小成把前还但只想中里用生种起知好些间因所如然后其最她它已当两从方实长更应什'
        )
    return _ZH_STOP_CHARS


def zh_extract_features(s: str) -> set:
    """从文本提取中文特征字 + 英文词干（去停用字后）。"""
    s = s.lower()
    chars = set(re.findall(r'[一-鿿]', s)) - zh_stop_chars()
    for t in re.findall(r'[a-z][a-z0-9]+', s):
        chars.add(t)
    return chars


# ═══════════════════════════════════════════════════════════════
# 5. state.json 读写
# ═══════════════════════════════════════════════════════════════

STATE_PATH = os.path.join(PROJECT_ROOT, "state.json")

def load_state() -> dict:
    return load_json_safe(STATE_PATH, default={}, label="state")

def save_state(state: dict):
    from delegate_tools import atomic_write_json
    atomic_write_json(STATE_PATH, state)


# ═══════════════════════════════════════════════════════════════
# 6. config.json 加载（带缓存）
# ═══════════════════════════════════════════════════════════════

_config_cache = None

def load_config() -> dict:
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    with open(config_path, "r", encoding="utf-8") as f:
        _config_cache = json.load(f)
    return _config_cache


def invalidate_config_cache():
    """强制下次 load_config() 重新读取文件。"""
    global _config_cache
    _config_cache = None


def record_bark_push(msg: str):
    """记录最近 N 条 Bark 推送消息到 state.json，供 trigger 注入主 AI prompt。"""
    state = load_state()
    recent = state.get("recent_bark", [])
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    bj_now = (_dt.now(_tz.utc) + _td(hours=8)).strftime('%m-%d %H:%M')
    recent.append({"time": bj_now, "msg": msg})
    state["recent_bark"] = recent[-5:]  # 只保留最近 5 条
    save_state(state)


def is_garbage_card(title: str, content: str) -> str:
    """检测占位/空卡片。返回空字符串=通过，否则返回拦截原因。"""
    _garbage = {"无明确承诺", "暂无计划", "没什么", "不知道", "无", "暂无", "未发现", "无承诺",
                "未在对话中承诺", "无明确事项", "无任何承诺"}
    if not title or not title.strip():
        return "空标题"
    if not content or not content.strip():
        return "空内容"
    if title.strip() in _garbage or content.strip() in _garbage:
        return f"占位标题: {title}"
    if "未在对话中" in content or "没有承诺" in content:
        return f"空内容模式: {content[:40]}"
    return ""


def get_recent_bark() -> list:
    """读取最近 Bark 推送记录（北京时间）。state.json 为空时降级扫 trigger.log。"""
    recent = load_state().get("recent_bark", [])
    if recent:
        return recent
    # 降级：从 trigger.log 提取最近 2 条成功推送，UTC → 北京时间
    import json as _j, os as _os
    from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2
    log_path = _os.path.join(PROJECT_ROOT, "trigger.log")
    if not _os.path.exists(log_path):
        return []
    try:
        with open(log_path, "r", encoding="utf-8") as _f:
            lines = _f.readlines()
        found = []
        for line in reversed(lines):
            try:
                e = _j.loads(line.strip())
            except Exception:
                continue
            if e.get("bark_sent") and e.get("bark_message", "").strip():
                # UTC → 北京时间
                raw_ts = e.get("timestamp", "")
                try:
                    utc_dt = _dt2.fromisoformat(raw_ts.replace("+0000", "+00:00"))
                    bj_dt = utc_dt.astimezone(_tz2(_td2(hours=8)))
                    time_str = bj_dt.strftime('%m-%d %H:%M')
                except Exception:
                    time_str = raw_ts[:16].replace("T", " ")
                found.append({
                    "time": time_str,
                    "msg": e["bark_message"][:100]
                })
                if len(found) >= 2:
                    break
        found.reverse()
        return found
    except Exception:
        return []


# ═══════════════════════════════════════════════════════════════
# 7. 追踪日志（统一断点，grep [TRACE] 即可）
# ═══════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════
# 回合状态面板：收集每轮事件，统一显示
# ═══════════════════════════════════════════════════════════════

_round_events = []

def status_event(tag: str, detail: str, icon: str = "•"):
    """记录回合内事件，供 flush_status() 统一显示。"""
    from datetime import datetime as _dt
    _round_events.append((tag, detail, icon))
    # 同时落盘到 trigger.log
    try:
        log_path = os.path.join(PROJECT_ROOT, "trigger.log")
        with open(log_path, "a", encoding="utf-8") as _tf:
            _tf.write(json.dumps({"timestamp": _dt.now().isoformat(), "event": "round_event", "tag": tag, "detail": detail}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def flush_status() -> str:
    """输出当前回合的状态面板，清空事件缓存。"""
    if not _round_events:
        return ""
    width = 56
    lines = [f"┌{'─' * width}┐"]
    for tag, detail, icon in _round_events:
        header = f"{icon} {tag}"
        content = detail[:width - len(header) - 3]
        lines.append(f"│ {header}: {content.ljust(width - len(header) - 2)}│")
    lines.append(f"└{'─' * width}┘")
    _round_events.clear()
    return "\n".join(lines)


def trace(tag: str, detail: str = ""):
    """独立追踪：直接打印 + 落盘。不进入回合状态面板。
    供 polling_loop / 后台进程使用。"""
    from datetime import datetime as _dt
    ts = _dt.now().strftime('%H:%M:%S.%f')[:12]
    msg = f"[TRACE {ts} {tag}] {detail}"
    print(msg)
    try:
        log_path = os.path.join(PROJECT_ROOT, "trigger.log")
        with open(log_path, "a", encoding="utf-8") as _tf:
            _tf.write(json.dumps({"timestamp": _dt.now().isoformat(), "event": "trace", "tag": tag, "detail": detail}, ensure_ascii=False) + "\n")
    except Exception:
        pass
