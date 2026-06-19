import os
import threading
import time
import traceback
from datetime import datetime

from ds_log import info, warn, error as log_error
from clock import beijing_now, beijing_today, beijing_yesterday, BJT
from delegate.dreaming import chain_dream, weekly_sweep
from memory.memory_manager import run_audit
from persona.miner import main as miner_main
from work_log import from_claude_sessions

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

TASK_TIMEOUTS = {
    "audit": 120,
    "work_extract": 300,
    "diary": 180,
    "miner": 120,
    "weekly": 180,
}


class ScheduledTasks:
    def __init__(self, api_guard):
        self._last_tick = 0.0
        self._last_daily = ""
        self._last_weekly = ""
        self._api_guard = api_guard
        self._state = {
            "last_audit": None, "last_work_extract": None,
            "last_diary": None, "last_miner": None, "last_weekly": None,
            "window": "unknown", "daily_done": False,
        }

    @staticmethod
    def _run_with_timeout(func, timeout_sec: float, name: str):
        result = [None]
        exc = [None]

        def _runner():
            try:
                result[0] = func()
            except Exception as e:
                exc[0] = e

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=timeout_sec)

        if t.is_alive():
            msg = f"{name} 超时 ({timeout_sec}s)，daemon 继续运行"
            warn("scheduled", msg)
            try:
                from crash_reporter import _write_crash_log
                _write_crash_log(f"[SCHEDULED_TIMEOUT] {name} >{timeout_sec}s\n{msg}")
            except Exception:
                pass
            return None

        if exc[0] is not None:
            raise exc[0]

        return result[0]

    def state_snapshot(self) -> dict:
        return dict(self._state)

    def _check_output_fresh(self, path: str, max_age_s: int) -> tuple:
        try:
            if not os.path.exists(path):
                return False, False, ""
            mtime = os.path.getmtime(path)
            age = time.time() - mtime
            mtime_str = datetime.fromtimestamp(mtime, BJT).isoformat()
            return True, age <= max_age_s, mtime_str
        except Exception:
            return False, False, ""

    def _run_audit_with_check(self, interval_s: int):
        anchor_path = os.path.join(PROJECT_ROOT, "memory", "anchor_set.json")
        exists, fresh, mtime_str = self._check_output_fresh(anchor_path, interval_s)

        if fresh:
            info("scheduled", f"审计跳过 — 锚定集合近期已更新 ({mtime_str})")
            self._state["last_audit"] = mtime_str
            return

        info("scheduled", f"执行审计... (上次: {mtime_str or '无记录'})")
        try:
            self._run_with_timeout(run_audit, TASK_TIMEOUTS["audit"], "audit")
            now_str = datetime.now(BJT).isoformat()
            self._state["last_audit"] = now_str
            _, fresh_after, _ = self._check_output_fresh(anchor_path, 60)
            if not fresh_after:
                info("scheduled", f"未找到审计变更，沐泽可能很忙 (审计已执行 @{now_str})")
        except Exception as e:
            log_error("scheduled", f"审计失败: {e}", exception_type=type(e).__name__)
            traceback.print_exc()

    def _run_work_extract_with_check(self, today_str: str, yesterday_str: str, interval_s: int):
        diary_path = os.path.join(PROJECT_ROOT, "diary", "work", f"{today_str}_work.md")
        exists, fresh, mtime_str = self._check_output_fresh(diary_path, max(interval_s, 1800))

        info("scheduled", f"提取工作总结... (日记上次产出: {mtime_str or '无记录'})")
        try:
            r1 = self._run_with_timeout(
                lambda: from_claude_sessions(today_str),
                TASK_TIMEOUTS["work_extract"], f"work_extract({today_str})")
            r2 = self._run_with_timeout(
                lambda: from_claude_sessions(yesterday_str),
                TASK_TIMEOUTS["work_extract"], f"work_extract({yesterday_str})")
            now_str = datetime.now(BJT).isoformat()
            self._state["last_work_extract"] = now_str
            if (r1 or 0) > 0 or (r2 or 0) > 0:
                info("scheduled", f"工作总结: today={r1}, yesterday={r2}")
            else:
                info("scheduled", f"未找到日志提取，沐泽可能很忙 (检查时间 @{now_str})")
        except Exception as e:
            log_error("scheduled", f"工作总结提取失败: {e}", exception_type=type(e).__name__)
            traceback.print_exc()

    def _run_diary_with_check(self, yesterday: str):
        diary_path = os.path.join(PROJECT_ROOT, "diary", f"{yesterday}.md")
        exists, fresh, mtime_str = self._check_output_fresh(diary_path, 86400)

        if exists:
            fsize = os.path.getsize(diary_path)
            info("scheduled", f"日记 {yesterday} 已存在 ({fsize}B, {mtime_str})，跳过")
            self._state["last_diary"] = mtime_str
            return

        info("scheduled", f"日记缺失 — {yesterday} 生成中... (上次: {mtime_str or '无'})")
        try:
            self._run_with_timeout(
                lambda: chain_dream(yesterday),
                TASK_TIMEOUTS["diary"], f"chain_dream({yesterday})")
            now_str = datetime.now(BJT).isoformat()
            self._state["last_diary"] = now_str
            if not os.path.exists(diary_path) or os.path.getsize(diary_path) < 50:
                info("scheduled", f"未找到日记生成，沐泽可能很忙 (生成尝试 @{now_str})")
            else:
                info("scheduled", f"日记 {yesterday} 生成完成 ({os.path.getsize(diary_path)}B)")
        except Exception as e:
            log_error("scheduled", f"日记生成失败: {e}", exception_type=type(e).__name__)
            traceback.print_exc()

    def _run_miner_with_check(self):
        miner_state_path = os.path.join(PROJECT_ROOT, "persona", "miner_state.json")
        exists, fresh, mtime_str = self._check_output_fresh(miner_state_path, 86400)

        info("scheduled", f"执行礦工压缩... (上次: {mtime_str or '无记录'})")
        try:
            self._run_with_timeout(miner_main, TASK_TIMEOUTS["miner"], "miner")
            now_str = datetime.now(BJT).isoformat()
            self._state["last_miner"] = now_str
            _, fresh_after, _ = self._check_output_fresh(miner_state_path, 120)
            if not fresh_after:
                info("scheduled", f"未找到礦工压缩，沐泽可能很忙 (执行时间 @{now_str})")
        except Exception as e:
            log_error("scheduled", f"礦工压缩失败: {e}", exception_type=type(e).__name__)
            traceback.print_exc()

    def _run_weekly_with_check(self, today_str: str):
        if not self._last_weekly:
            self._last_weekly = today_str
        try:
            last_wk = datetime.strptime(self._last_weekly, "%Y-%m-%d").date()
            today_d = datetime.strptime(today_str, "%Y-%m-%d").date()
            days_since = (today_d - last_wk).days
        except Exception:
            return

        if days_since < 7:
            return

        info("scheduled", f"执行每周收拢... (上次: {self._last_weekly}, 距今天 {days_since}d)")
        try:
            result_path = self._run_with_timeout(weekly_sweep, TASK_TIMEOUTS["weekly"], "weekly_sweep")
            now_str = datetime.now(BJT).isoformat()
            self._state["last_weekly"] = now_str
            self._last_weekly = today_str
            if not result_path or not os.path.exists(result_path):
                info("scheduled", f"未找到周记收拢，沐泽可能很忙 (执行时间 @{now_str})")
            else:
                info("scheduled", f"周记收拢完成: {result_path}")
        except Exception as e:
            log_error("scheduled", f"周记收拢失败: {e}", exception_type=type(e).__name__)
            traceback.print_exc()

    def tick(self, now_ts: float, boot_grace_sec: float, started_at_ts: float):
        if time.time() - started_at_ts < boot_grace_sec:
            return

        bj_hour = beijing_now().hour
        today_str = beijing_today()
        yesterday_str = beijing_yesterday()

        sleep_window = 3 <= bj_hour < 12
        peak_window = 0 <= bj_hour < 3
        interval = 3600 if sleep_window or peak_window else 7200

        if now_ts - self._last_tick < interval:
            return
        self._last_tick = now_ts

        if sleep_window:
            self._state["window"] = "sleep"
            info("scheduled", "休眠窗口 (3am-12pm)，跳过任务")
            return

        is_daily_due = peak_window and self._last_daily != today_str
        self._state["window"] = "peak(daily)" if is_daily_due else ("peak" if peak_window else "day")

        self._run_audit_with_check(interval)
        self._run_work_extract_with_check(today_str, yesterday_str, interval)

        if is_daily_due:
            self._last_daily = today_str
            self._state["daily_done"] = True
            yesterday = yesterday_str
            self._run_diary_with_check(yesterday)
            self._run_miner_with_check()
            self._run_weekly_with_check(today_str)
