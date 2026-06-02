"""
persona_engine.py — DS 对沐泽的情感状态追踪

读 va_log.jsonl + manual_va.json，计算累积情感指标：
  affinity   (亲近度) — 互动频率+质量，连续正效价↑，长期冷落↓
  tenderness (温柔度) — 深层话题驱动，平静期缓慢回落

每 6h audit 时更新一次，console.py 支持手动 ±0.05 微调。
"""
import json
import os
from datetime import datetime, timezone, timedelta

BJT = timezone(timedelta(hours=8))
STATE_PATH = os.path.join(os.path.dirname(__file__), "persona_state.json")
VA_LOG_PATH = os.path.join(os.path.dirname(__file__), "va_log.jsonl")
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))

DEFAULT_STATE = {
    "affinity": 0.60,
    "tenderness": 0.55,
    "last_updated": "",
    "trend_7d": "stable",
    "notes": "",
    "manual_touch": False,
}


def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                s = json.load(f)
            for k, v in DEFAULT_STATE.items():
                s.setdefault(k, v)
            return s
        except Exception:
            pass
    return dict(DEFAULT_STATE)


def save_state(state):
    from delegate_tools import atomic_write_json
    atomic_write_json(STATE_PATH, state)


def _read_va_log(days=7):
    """读最近 N 天 VA 估测记录，返回 [(datetime, valence, arousal), ...]"""
    if not os.path.exists(VA_LOG_PATH):
        return []
    cutoff = datetime.now(BJT) - timedelta(days=days)
    records = []
    try:
        with open(VA_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    entry = json.loads(line.strip())
                    ts_str = entry.get("timestamp", "") or entry.get("ts", "")
                    if not ts_str:
                        continue
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    ts = ts.astimezone(BJT).replace(tzinfo=None)
                    if ts >= cutoff.replace(tzinfo=None):
                        records.append((ts, entry.get("valence", 0), entry.get("arousal", 0.5)))
                except (json.JSONDecodeError, ValueError):
                    continue
    except Exception:
        pass
    return records


def _read_manual_va():
    """读手动 VA 设置。"""
    path = os.path.join(PROJECT_ROOT, "manual_va.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def update_persona():
    """计算并更新 persona_state。由 run_audit 每 6h 调用一次。"""
    state = load_state()
    records = _read_va_log(days=7)
    manual = _read_manual_va()

    if not records:
        state["last_updated"] = datetime.now(BJT).isoformat()
        state["notes"] = "(无VA数据)"
        save_state(state)
        return state

    # 计算 7 天 VA 趋势
    valences = [r[1] for r in records]
    arousals = [r[2] for r in records]
    avg_v = sum(valences) / len(valences)
    avg_a = sum(arousals) / len(arousals)

    # 近期偏正还是偏负（最近 3 天 vs 前 4 天）
    mid = min(3, len(records) // 2)
    recent_v = sum(r[1] for r in records[-mid:]) / max(1, mid)
    older_v = sum(r[1] for r in records[:len(records)-mid]) / max(1, len(records)-mid)
    delta_v = recent_v - older_v

    # affinity: 受 VA 均值 + 近期趋势影响
    # 正效价持久 → +0.02，负效价持续 → -0.02，趋势变化 → ±0.01
    affinity_delta = 0.0
    if avg_v > 0.2:
        affinity_delta += 0.02
    elif avg_v < -0.2:
        affinity_delta -= 0.02
    if delta_v > 0.2:
        affinity_delta += 0.01
    elif delta_v < -0.2:
        affinity_delta -= 0.01

    # tenderness: 受高唤醒+深层话题驱动
    # 高唤醒(>0.5)回合多 → tenderness 涨；全是低唤醒 → 回落
    tenderness_delta = 0.0
    high_a_ratio = sum(1 for a in arousals if a > 0.5) / max(1, len(arousals))
    if high_a_ratio > 0.4:
        tenderness_delta += 0.02
    elif high_a_ratio < 0.15:
        tenderness_delta -= 0.01

    # 手动 VA 覆盖（信任度权重）
    if manual.get("enabled") and not state.get("manual_touch"):
        trust = manual.get("trust", 0.8)
        manual_v = manual.get("valence", 0.0)
        manual_a = manual.get("arousal", 0.5)
        # 手动 VA 影响 affinity（通过效价）和 tenderness（通过唤醒）
        if manual_v > 0.3:
            affinity_delta += 0.03 * trust
        elif manual_v < -0.3:
            affinity_delta -= 0.03 * trust
        if manual_a > 0.5:
            tenderness_delta += 0.02 * trust

    # 应用到当前值，限幅 [0, 1]
    affinity = max(0.0, min(1.0, state["affinity"] + affinity_delta))
    tenderness = max(0.0, min(1.0, state["tenderness"] + tenderness_delta))

    # 趋势判断
    if delta_v > 0.15:
        trend = "warming"
    elif delta_v < -0.15:
        trend = "cooling"
    else:
        trend = "stable"

    state["affinity"] = round(affinity, 3)
    state["tenderness"] = round(tenderness, 3)
    state["trend_7d"] = trend
    state["last_updated"] = datetime.now(BJT).isoformat()
    state["notes"] = f"avg_v={avg_v:.2f} avg_a={avg_a:.2f} delta_v={delta_v:.2f}"
    state["manual_touch"] = False  # 审计时重置，等下一次手动触摸

    save_state(state)
    print(f"[persona] affinity={affinity:.3f} tenderness={tenderness:.3f} trend={trend}")
    return state


def manual_adjust(key, delta):
    """手动微调 persona 指标（console.py 调用）。"""
    state = load_state()
    if key in ("affinity", "tenderness"):
        state[key] = round(max(0.0, min(1.0, state.get(key, 0.5) + delta)), 3)
        state["manual_touch"] = True
        state["last_updated"] = datetime.now(BJT).isoformat()
        save_state(state)
    return state


def persona_prompt_snippet():
    """生成注入 system prompt 的一句话。"""
    state = load_state()
    trend_map = {"warming": "回暖中", "cooling": "回落中", "stable": "平稳"}
    return (
        f"【对沐泽的感情状态】亲近度={state['affinity']:.2f} "
        f"温柔度={state['tenderness']:.2f} "
        f"({trend_map.get(state['trend_7d'], '平稳')})"
    )
