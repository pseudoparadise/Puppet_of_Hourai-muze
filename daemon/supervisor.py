import os
import subprocess
import sys
import time
from dataclasses import dataclass, field

from ds_log import info, warn, error
from boot_guard import write_pid_with_boot_token
from .preflight import is_pid_alive

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = sys.executable


@dataclass
class ChildConfig:
    name: str
    script: str
    heartbeat_file: str | None = None
    heartbeat_max_age: float = 120.0
    max_crashes: int = 5
    crash_window: float = 300.0
    base_delay: float = 10.0
    max_delay: float = 300.0

    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _crash_times: list[float] = field(default_factory=list, repr=False)
    _exhausted: bool = field(default=False, repr=False)

    def full_script_path(self) -> str:
        return os.path.join(PROJECT_ROOT, self.script)


class Supervisor:
    def __init__(self):
        self._children: list[ChildConfig] = []
        self._last_state_write = 0.0
        self._state_cooldown = 30.0
        self._boot_token = None

    def add_child(self, config: ChildConfig):
        self._children.append(config)

    def start(self, boot_token: int):
        self._boot_token = boot_token
        for child in self._children:
            self._spawn(child)

    def tick(self) -> dict:
        """返回状态快照供 console/daemon state 使用。"""
        states = {}
        for child in self._children:
            states[child.name] = self._check_and_restart(child)
        return states

    def shutdown(self):
        for child in self._children:
            if child._proc and child._proc.poll() is None:
                child._proc.terminate()
                try:
                    child._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child._proc.kill()
                info("supervisor", f"{child.name} 已停止")

    def _spawn(self, child: ChildConfig):
        try:
            child._proc = subprocess.Popen(
                [PYTHON, child.full_script_path()],
                cwd=PROJECT_ROOT,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            info("supervisor", f"{child.name} 已启动", pid=child._proc.pid)
        except Exception as e:
            error("supervisor", f"{child.name} 启动失败", error=str(e))

    def _check_and_restart(self, child: ChildConfig) -> dict:
        if child._exhausted:
            return {"pid": None, "state": "exhausted", "crashes_recent": len(child._crash_times)}

        need_restart = False
        reason = ""

        if child._proc is None:
            need_restart = True
            reason = "从未启动"
        elif child._proc.poll() is not None:
            need_restart = True
            reason = f"进程已退出 (code={child._proc.returncode})"
        elif child.heartbeat_file:
            hb_path = os.path.join(PROJECT_ROOT, child.heartbeat_file)
            try:
                age = time.time() - os.path.getmtime(hb_path)
                if age > child.heartbeat_max_age:
                    need_restart = True
                    reason = f"心跳超时 ({int(age)}s)"
                    child._proc.kill()
                    try:
                        child._proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        pass
            except FileNotFoundError:
                if child._proc.poll() is not None:
                    need_restart = True
                    reason = "心跳文件缺失且进程已死"
            except Exception:
                pass

        if not need_restart:
            hb_age = None
            if child.heartbeat_file:
                try:
                    hb_age = time.time() - os.path.getmtime(os.path.join(PROJECT_ROOT, child.heartbeat_file))
                except Exception:
                    pass
            return {
                "pid": child._proc.pid if child._proc else None,
                "state": "running",
                "crashes_recent": len(child._crash_times),
                "last_heartbeat_age_s": int(hb_age) if hb_age else None,
            }

        now = time.time()
        child._crash_times = [t for t in child._crash_times if now - t < child.crash_window]
        child._crash_times.append(now)
        crash_count = len(child._crash_times)

        if crash_count > child.max_crashes:
            child._exhausted = True
            error("supervisor", f"{child.name} {child.crash_window:.0f}s 内崩溃 {crash_count} 次，停止重试")
            from .panic import panic_popup
            panic_popup(
                f"{child.name}",
                f"{child.crash_window:.0f}s 内崩溃 {crash_count} 次\n请检查 {child.script} 是否正常"
            )
            return {"pid": None, "state": "exhausted", "crashes_recent": crash_count}

        delay = min(child.base_delay * (2 ** (crash_count - 1)), child.max_delay)
        warn("supervisor", f"{child.name} {reason}，{delay}s 后重拉 (崩溃#{crash_count})")
        time.sleep(delay)
        self._spawn(child)

        return {
            "pid": child._proc.pid if child._proc else None,
            "state": "running",
            "crashes_recent": crash_count,
        }

    def child_state_snapshot(self, name: str) -> dict:
        for child in self._children:
            if child.name == name:
                return {
                    "pid": child._proc.pid if child._proc else None,
                    "state": "exhausted" if child._exhausted else ("running" if child._proc and child._proc.poll() is None else "stopped"),
                    "crashes_recent": len(child._crash_times),
                }
        return {}
