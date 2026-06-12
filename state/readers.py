import json
import os

from .store import load

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_daemon_health() -> dict:
    """返回守护进程健康状态，兼容 console.py 旧格式。"""
    try:
        s = load()
        return {
            "daemon_pid": s.daemon.pid,
            "uptime_seconds": s.daemon.uptime_seconds,
            "music_poll": {
                "pid": s.music.pid,
                "state": s.music.state,
                "crashes_recent": s.music.crashes_recent,
                "last_heartbeat_age_s": s.music.last_heartbeat_age_s,
            },
            "bark": {
                "last_run_time": s.bark.last_run_time,
                "state": s.bark.state,
                "crashes_recent": s.bark.crashes_recent,
            },
            "scheduled": {
                "last_audit": s.scheduled.last_audit,
                "last_work_extract": s.scheduled.last_work_extract,
                "last_diary": s.scheduled.last_diary,
                "last_miner": s.scheduled.last_miner,
                "last_weekly": s.scheduled.last_weekly,
                "window": s.scheduled.window,
                "daily_done": s.scheduled.daily_done,
            },
            "api_calls_this_hour": s.api.calls_this_hour,
            "errors": s.errors,
            "last_updated": s.last_updated,
        }
    except Exception:
        return _fallback_daemon_state()


def _fallback_daemon_state() -> dict:
    """读取旧 .daemon_state.json 作为降级方案。"""
    path = os.path.join(PROJECT_ROOT, ".daemon_state.json")
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def get_music_toggle() -> bool:
    """返回音乐开关状态。"""
    try:
        s = load()
        return s.music_toggle.get("enabled", False)
    except Exception:
        path = os.path.join(PROJECT_ROOT, ".music_toggle.json")
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f).get("enabled", False)
        except Exception:
            pass
    return False
