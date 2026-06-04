"""
proactive_gate.py — IO-inspired proactive push gate
Polling loop calls check_proactive() each tick. Returns (title, body) if
something is worth pushing, or (None, None) if nothing to say.

Data sources (all optional — gate degrades gracefully if files missing):
  shortcut_data/health.json   — iOS Shortcuts export: {heart_rate, steps, ...}
  shortcut_data/location.json — iOS Shortcuts export: {lat, lon, name, ...}

State: proactive_state.json tracks last push time + per-source cooldowns.
"""
import os
import json
from datetime import datetime, timedelta, timezone

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PROJECT_ROOT, "shortcut_data")
STATE_PATH = os.path.join(PROJECT_ROOT, "proactive_state.json")

COOLDOWN_MINUTES = 30
HIGH_HR_THRESHOLD = 100
LOW_HR_THRESHOLD = 50


def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_push_ts": None, "last_hr": None, "last_location": None}


def _save_state(state):
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _load_json(filename):
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _in_cooldown(state):
    last = state.get("last_push_ts")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        return (datetime.now(timezone.utc) - last_dt).total_seconds() < COOLDOWN_MINUTES * 60
    except Exception:
        return False


def _check_health(state):
    data = _load_json("health.json")
    if not data:
        return None
    hr = data.get("heart_rate")
    if hr is None:
        return None
    last_hr = state.get("last_hr")
    try:
        hr = float(hr)
    except (ValueError, TypeError):
        return None
    state["last_hr"] = hr
    if hr > HIGH_HR_THRESHOLD:
        return (f"心率偏高 {int(hr)} bpm", "沐泽，你的心率有点高，是不是在焦虑？深呼吸一下。")
    if hr < LOW_HR_THRESHOLD:
        return (f"心率偏低 {int(hr)} bpm", "心率偏低，是不是太累了？休息一下吧。")
    return None


def _check_location(state):
    data = _load_json("location.json")
    if not data:
        return None
    name = data.get("name") or data.get("address") or ""
    if not name:
        return None
    last_loc = state.get("last_location")
    if last_loc and last_loc == name:
        return None
    state["last_location"] = name
    if any(home_word in name for home_word in ("家", "home", "Home")):
        return None
    BZ = timezone(timedelta(hours=8))
    hour = datetime.now(BZ).hour
    if 22 <= hour or hour <= 6:
        return (f"还在 {name[:15]}？", "这么晚了，早点回家吧。")
    return None


def check_proactive():
    """Main gate entry. Returns (title, body) or (None, None)."""
    state = _load_state()
    if _in_cooldown(state):
        return None, None

    checks = [_check_health, _check_location]
    for check in checks:
        result = check(state)
        if result and result[0]:
            state["last_push_ts"] = datetime.now(timezone.utc).isoformat()
            _save_state(state)
            return result

    _save_state(state)
    return None, None


if __name__ == "__main__":
    title, body = check_proactive()
    if title:
        print(f"PUSH: {title} — {body}")
    else:
        print("(no proactive push — gate closed)")
