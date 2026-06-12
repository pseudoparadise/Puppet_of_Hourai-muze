import argparse
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime

from boot_guard import (
    get_boot_token, write_pid_with_boot_token,
    read_pid_with_boot_token, cleanup_stale_pid_file,
)
from clock import beijing_now, BJT
from delegate_tools import atomic_write_json
from ds_log import info, warn, error as log_error

from .api_guard import ApiGuard
from .bark_runner import BarkRunner
from .panic import panic_popup
from .preflight import preflight_cleanup, startup_cleanup, is_pid_alive
from .scheduled import ScheduledTasks
from .supervisor import ChildConfig, Supervisor

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PID_FILE = os.path.join(PROJECT_ROOT, ".daemon.pid")
STATE_FILE = os.path.join(PROJECT_ROOT, ".daemon_state.json")

BOOT_GRACE_SEC = 300
API_CALL_LIMIT_PER_HOUR = 30


def _daemon_state_template():
    return {
        "daemon_pid": os.getpid(),
        "boot_token": get_boot_token(),
        "started_at": datetime.now(BJT).isoformat(),
        "started_at_ts": time.time(),
        "uptime_seconds": 0,
        "music_poll": {"pid": None, "state": "stopped", "crashes_recent": 0, "last_heartbeat_age_s": None},
        "bark": {"last_run_time": None, "state": "idle", "crashes_recent": 0},
        "scheduled": {
            "last_audit": None, "last_work_extract": None,
            "last_diary": None, "last_miner": None, "last_weekly": None,
            "window": "unknown", "daily_done": False,
        },
        "api_calls_this_hour": 0,
        "errors": [],
        "last_updated": None,
    }


def _is_service_running(pid_file: str) -> bool:
    if not os.path.exists(pid_file):
        return False
    try:
        pid, bt = read_pid_with_boot_token(pid_file)
        if pid is None:
            os.remove(pid_file)
            return False
        if bt is not None and bt > get_boot_token():
            print(f"[daemon] {os.path.basename(pid_file)} 来自上一次启动，清理")
            os.remove(pid_file)
            return False
        if is_pid_alive(pid):
            return True
        os.remove(pid_file)
    except Exception:
        pass
    return False


def _remove_pid(path: str):
    try:
        os.remove(path)
    except Exception:
        pass


def run_daemon(args):
    if _is_service_running(PID_FILE):
        pid, _ = read_pid_with_boot_token(PID_FILE)
        print(f"[daemon] 已有守护进程运行 (PID {pid or '?'})")
        sys.exit(1)

    boot_token = get_boot_token()
    write_pid_with_boot_token(PID_FILE, os.getpid())
    preflight_cleanup()
    startup_cleanup(boot_token)

    ds = _daemon_state_template()
    ds["boot_token"] = boot_token

    has_music = not args.no_music
    has_bark = not args.no_bark
    has_scheduled = not args.no_scheduled

    print("DSphantom 守护进程启动 (重构版)")
    print(f"  PID: {os.getpid()}")
    print(f"  音乐: {'on' if has_music else 'off'}  bark: {'on' if has_bark else 'off'}  定时: {'on' if has_scheduled else 'off'}")
    print(f"  项目: {PROJECT_ROOT}")

    api_guard = ApiGuard(API_CALL_LIMIT_PER_HOUR)

    supervisor = Supervisor()
    if has_music:
        supervisor.add_child(ChildConfig(
            name="music_poll",
            script="music_poll.py",
            heartbeat_file=".music_state.json",
            heartbeat_max_age=120.0,
            max_crashes=5,
            crash_window=300.0,
            base_delay=10.0,
            max_delay=300.0,
        ))
    supervisor.start(boot_token)

    scheduled = ScheduledTasks(api_guard) if has_scheduled else None
    bark_runner = BarkRunner(api_guard) if has_bark else None

    _last_write = 0.0

    def _write_state(force=False):
        nonlocal _last_write
        now_t = time.time()
        if not force and now_t - _last_write < 30.0:
            return
        _last_write = now_t
        ds["uptime_seconds"] = int(time.time() - ds.get("started_at_ts", time.time()))
        ds["last_updated"] = datetime.now(BJT).isoformat()
        try:
            atomic_write_json(STATE_FILE, ds)
        except Exception:
            pass

    _write_state(force=True)

    def cleanup():
        print("\n[daemon] 正在停止所有服务...")
        supervisor.shutdown()
        _remove_pid(PID_FILE)
        print("[daemon] 已停止。")

    def _on_signal(sig, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    while True:
        loop_start = time.time()

        music_states = supervisor.tick()
        for name, child_state in music_states.items():
            ds["music_poll"] = {**ds["music_poll"], **child_state}

        if bark_runner:
            bark_state = bark_runner.tick(loop_start, BOOT_GRACE_SEC, ds["started_at_ts"])
            ds["bark"] = {**ds["bark"], **bark_state}

        if scheduled:
            scheduled.tick(loop_start, BOOT_GRACE_SEC, ds["started_at_ts"])
            ds["scheduled"] = scheduled.state_snapshot()

        ds["api_calls_this_hour"] = api_guard.calls_this_hour
        _write_state()

        elapsed = time.time() - loop_start
        if elapsed < 5:
            time.sleep(5 - elapsed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-music", action="store_true")
    parser.add_argument("--no-bark", action="store_true")
    parser.add_argument("--no-scheduled", action="store_true")
    args = parser.parse_args()

    DAEMON_RETRY_MAX = 3
    DAEMON_RETRY_WINDOW = 600
    DAEMON_RETRY_BASE = 5
    crash_times = []

    while True:
        try:
            run_daemon(args)
            break
        except SystemExit:
            break
        except KeyboardInterrupt:
            print("[daemon] 收到中断信号")
            break
        except Exception:
            now = time.time()
            crash_times = [t for t in crash_times if now - t < DAEMON_RETRY_WINDOW]
            crash_times.append(now)
            crash_count = len(crash_times)
            traceback.print_exc()
            log_error("daemon", f"守护进程崩溃 (#{crash_count})")

            if crash_count > DAEMON_RETRY_MAX:
                panic_popup("守护进程 daemon", f"{DAEMON_RETRY_WINDOW}s 内崩溃 {crash_count} 次\n已达重试上限，退出。")
                sys.exit(1)

            delay = min(DAEMON_RETRY_BASE * (2 ** (crash_count - 1)), 120)
            print(f"[daemon] {delay}s 后重试 daemon 主循环...")
            time.sleep(delay)


if __name__ == "__main__":
    from crash_reporter import install as _install_crash
    _install_crash()
    main()
