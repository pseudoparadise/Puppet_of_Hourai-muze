import json
import os
import time

from delegate_tools import atomic_write_json
from ds_log import info, warn

from .schema import DsphantomState, DaemonState, MusicPollState, BarkState, ScheduledState

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(PROJECT_ROOT, ".dsphantom_state.json")


_CACHE_MAX_AGE = 30.0
_migration_done = False


def load() -> DsphantomState:
    if os.path.exists(STATE_PATH):
        try:
            age = time.time() - os.path.getmtime(STATE_PATH)
            if age < _CACHE_MAX_AGE:
                with open(STATE_PATH, "r", encoding="utf-8") as f:
                    return DsphantomState.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError) as e:
            warn("state", f"统一状态文件损坏: {e}")

    return _migrate_from_legacy()


def _migrate_from_legacy() -> DsphantomState:
    global _migration_done
    state = DsphantomState()
    had_data = False

    daemon_path = os.path.join(PROJECT_ROOT, ".daemon_state.json")
    if os.path.exists(daemon_path):
        try:
            with open(daemon_path, "r", encoding="utf-8") as f:
                ds = json.load(f)
            state.daemon = DaemonState(
                pid=ds.get("daemon_pid"), boot_token=ds.get("boot_token"),
                started_at=ds.get("started_at", ""), started_at_ts=ds.get("started_at_ts", 0.0),
                uptime_seconds=ds.get("uptime_seconds", 0),
            )
            dm = ds.get("music_poll", {})
            state.music = MusicPollState(
                pid=dm.get("pid"), state=dm.get("state", "stopped"),
                crashes_recent=dm.get("crashes_recent", 0),
                last_heartbeat_age_s=dm.get("last_heartbeat_age_s"),
            )
            db = ds.get("bark", {})
            state.bark = BarkState(
                last_run_time=db.get("last_run_time"), state=db.get("state", "idle"),
                crashes_recent=db.get("crashes_recent", 0),
            )
            dsc = ds.get("scheduled", {})
            state.scheduled = ScheduledState(
                last_audit=dsc.get("last_audit"), last_work_extract=dsc.get("last_work_extract"),
                last_diary=dsc.get("last_diary"), last_miner=dsc.get("last_miner"),
                last_weekly=dsc.get("last_weekly"), window=dsc.get("window", "unknown"),
                daily_done=dsc.get("daily_done", False),
            )
            state.errors = ds.get("errors", [])
            had_data = True
        except Exception as e:
            warn("state", f"读取 .daemon_state.json 失败: {e}")

    toggle_path = os.path.join(PROJECT_ROOT, ".music_toggle.json")
    if os.path.exists(toggle_path):
        try:
            with open(toggle_path, "r", encoding="utf-8") as f:
                state.music_toggle = json.load(f)
        except Exception:
            pass

    session_path = os.path.join(PROJECT_ROOT, ".session_state.json")
    if os.path.exists(session_path):
        try:
            with open(session_path, "r", encoding="utf-8") as f:
                ss = json.load(f)
            state.active_time = ss.get("last_user_message_time") or ss.get("active_time")
        except Exception:
            pass

    if not _migration_done and had_data:
        info("state", "从旧状态文件迁移完成，后续 30s 内静默读取")
        _migration_done = True

    save(state)
    return state


def save(state: DsphantomState):
    state.last_updated = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
    try:
        atomic_write_json(STATE_PATH, state.to_dict())
    except Exception as e:
        warn("state", f"写入统一状态文件失败: {e}")
