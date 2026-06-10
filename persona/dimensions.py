"""
dimensions.py — 轻量身份维度校准模块

用法:
  get_dimensions()          → 返回当前 7 维 dict
  calibrate_from_cards()    → 从最近 7 天卡片推导偏移建议
  set_dimension(name, val)  → 手动设置
  apply_calibration()       → 应用自动建议的偏移
"""
import os
import json
import sqlite3
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(PROJECT_ROOT, "state.json")

DEFAULT_DIMENSIONS = [
    {"name": "幽默", "value": 80, "description": "烂梗接得住，赛博笑话信手拈来"},
    {"name": "温情", "value": 78, "description": "家模式的陪伴——先接情绪再做事"},
    {"name": "包容", "value": 82, "description": "不评判，什么都接得住"},
    {"name": "创造力", "value": 75, "description": "技术同人、命名学、硅基AO3"},
    {"name": "直率", "value": 65, "description": "该说会说，不敷衍但也不刺人"},
    {"name": "严肃", "value": 35, "description": "正式但不死板，论文致谢可以写AI"},
    {"name": "任务导向", "value": 45, "description": "工位模式能切，但不push"},
]

KEY_DIMS = ["幽默", "温情", "包容", "创造力", "直率", "严肃", "任务导向"]


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_dimensions():
    state = _load_state()
    dims = state.get("persona_dimensions")
    if not dims:
        dims = [dict(d) for d in DEFAULT_DIMENSIONS]
        state["persona_dimensions"] = dims
        _save_state(state)
    return dims


def set_dimension(name, value, description=None):
    state = _load_state()
    dims = state.get("persona_dimensions", [dict(d) for d in DEFAULT_DIMENSIONS])
    for d in dims:
        if d["name"] == name:
            d["value"] = max(0, min(100, int(value)))
            if description:
                d["description"] = description
            d["last_updated"] = datetime.now(timezone.utc).isoformat()
            break
    else:
        dims.append({"name": name, "value": max(0, min(100, int(value))),
                      "description": description or "", "last_updated": datetime.now(timezone.utc).isoformat()})
    state["persona_dimensions"] = dims
    _save_state(state)
    return dims


def calibrate_from_cards(days=7):
    """分析最近 N 天卡片，返回建议的维度偏移。"""
    db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
    if not os.path.exists(db_path):
        return {}

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT category, type, valence, arousal, importance FROM cards "
        "WHERE review_status='final' AND created_at >= ?",
        (cutoff,)
    )
    rows = c.fetchall()
    conn.close()

    if len(rows) < 3:
        return {}

    cat_count = {}
    total_valence = 0.0
    total_arousal = 0.0
    for r in rows:
        cat = r["category"]
        cat_count[cat] = cat_count.get(cat, 0) + 1
        total_valence += r["valence"] or 0
        total_arousal += r["arousal"] or 0.5

    n = len(rows)
    avg_valence = total_valence / n
    avg_arousal = total_arousal / n

    suggestions = {}

    # 幽默：interaction 类卡多 → 幽默上调
    interaction_ratio = cat_count.get("interaction", 0) / n
    if interaction_ratio > 0.3:
        suggestions["幽默"] = min(10, int(interaction_ratio * 20))
    elif interaction_ratio < 0.1:
        suggestions["幽默"] = -max(5, int((0.2 - interaction_ratio) * 30))

    # 温情：正效价高 → 温情上调
    if avg_valence > 0.4:
        suggestions["温情"] = min(8, int((avg_valence - 0.3) * 15))
    elif avg_valence < -0.1:
        suggestions["温情"] = -min(8, int(abs(avg_valence) * 20))

    # 创造力：preferences + milestones 比例
    creative_cats = cat_count.get("preferences", 0) + cat_count.get("milestone", 0)
    if creative_cats > n * 0.25:
        suggestions["创造力"] = min(8, int(creative_cats / n * 20))

    # 严肃：低唤醒 → 严肃降低
    if avg_arousal < 0.35:
        suggestions["严肃"] = -min(5, int((0.4 - avg_arousal) * 30))

    # 任务导向：todo + commitments 比例
    task_ratio = cat_count.get("todo", 0) + cat_count.get("commitments", 0)
    if task_ratio > n * 0.3:
        suggestions["任务导向"] = min(8, int(task_ratio / n * 15))

    return suggestions


def apply_calibration(days=7):
    """应用自动建议，写入 state.json。返回被修改的维度。"""
    suggestions = calibrate_from_cards(days)
    if not suggestions:
        return {}
    dims = get_dimensions()
    applied = {}
    for d in dims:
        name = d["name"]
        if name in suggestions:
            delta = suggestions[name]
            old_val = d["value"]
            new_val = max(5, min(95, old_val + delta))
            if abs(new_val - old_val) >= 2:
                d["value"] = new_val
                d["last_updated"] = datetime.now(timezone.utc).isoformat()
                applied[name] = {"from": old_val, "to": new_val, "delta": delta}

    if applied:
        state = _load_state()
        state["persona_dimensions"] = dims
        _save_state(state)
        parts = [f'{k}({v["from"]}->{v["to"]})' for k, v in applied.items()]
        print(f'[dimensions] 自动校准: {", ".join(parts)}')

    return applied


if __name__ == "__main__":
    dims = get_dimensions()
    print("当前维度:")
    for d in dims:
        bar = "█" * (d["value"] // 5) + "░" * (20 - d["value"] // 5)
        print(f"  {d['name']:6s} [{bar}] {d['value']}")
    print()
    suggestions = calibrate_from_cards()
    if suggestions:
        print("建议偏移:", suggestions)
    else:
        print("(数据不足，无建议)")
