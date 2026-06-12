import os
import subprocess
import sys
import time
import traceback
from datetime import datetime

from ds_log import info, warn, error
from clock import beijing_now, BJT

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable
BARK_SCRIPT = os.path.join(PROJECT_ROOT, "bark_trigger.py")


class BarkRunner:
    def __init__(self, api_guard):
        self._last_run = 0.0
        self._api_guard = api_guard
        self._crashes_recent = 0

    def _get_interval(self, hour: int) -> int:
        if 0 <= hour < 3:
            return 60
        elif 3 <= hour < 12:
            return 600
        else:
            return 180

    def tick(self, now_ts: float, boot_grace_sec: float, started_at_ts: float) -> dict:
        if time.time() - started_at_ts < boot_grace_sec:
            return {"state": "grace", "last_run_time": None}

        bj_h = beijing_now().hour
        interval = self._get_interval(bj_h)
        if now_ts - self._last_run < interval:
            return {"state": "idle", "last_run_time": None}

        self._last_run = now_ts

        if not self._api_guard.check():
            warn("bark", "API 钱包守卫拦截 — 本小时已达限额")
            return {"state": "throttled", "last_run_time": None}

        info("bark", "执行 bark_trigger 检查...")
        try:
            proc = subprocess.run(
                [PYTHON, BARK_SCRIPT],
                cwd=PROJECT_ROOT,
                capture_output=True, text=True,
                timeout=120,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if proc.returncode != 0:
                self._crashes_recent += 1
                error("bark", f"bark_trigger 异常退出", returncode=proc.returncode, stderr=proc.stderr[-200:])
            else:
                self._crashes_recent = max(0, self._crashes_recent - 1)

            last_run_time = datetime.now(BJT).isoformat()
            return {
                "state": "ok" if proc.returncode == 0 else "error",
                "last_run_time": last_run_time,
                "crashes_recent": self._crashes_recent,
            }
        except subprocess.TimeoutExpired:
            self._crashes_recent += 1
            error("bark", "bark_trigger 超时 (120s)")
            return {"state": "timeout", "last_run_time": None, "crashes_recent": self._crashes_recent}
        except Exception as e:
            self._crashes_recent += 1
            error("bark", f"bark_trigger 启动失败: {e}")
            traceback.print_exc()
            return {"state": "error", "last_run_time": None, "crashes_recent": self._crashes_recent}
