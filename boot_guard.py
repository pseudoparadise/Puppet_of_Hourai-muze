"""
boot_guard.py — 启动守卫：用 GetTickCount64 作为启动指纹，防止重启后 PID 复用误判。
所有 PID 文件改为 {"pid": 18224, "boot_token": 12} 格式，兼容旧纯整数格式。

用法：
    from boot_guard import (
        get_boot_token,
        read_pid_with_boot_token,
        write_pid_with_boot_token,
        cleanup_stale_pid_file,
    )
"""
import json
import os
import tempfile
import shutil
import ctypes


def get_boot_token() -> int:
    """返回当前启动的 '开机小时数'。
    GetTickCount64 从开机开始计时，毫秒递增，重启后归零。
    用 3600000ms(1小时) 做粒度，48 天 uptime 也不会回绕。
    """
    tick = ctypes.windll.kernel32.GetTickCount64()
    return int(tick // 3_600_000)  # 毫秒 → 小时


def read_pid_with_boot_token(path: str) -> tuple:
    """读取 PID 文件，返回 (pid, boot_token)。
    兼容旧格式（纯整数 PID）→ boot_token 返回 None。
    文件不存在或损坏 → 返回 (None, None)。
    """
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        data = json.loads(raw)
        if isinstance(data, dict):
            return data.get("pid"), data.get("boot_token")
        # 旧格式：纯整数
        return int(raw), None
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    except Exception:
        pass
    return None, None


def write_pid_with_boot_token(path: str, pid: int):
    """原子写入 PID 文件（含 boot_token）。"""
    data = {"pid": pid, "boot_token": get_boot_token()}
    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix=".pid",
        prefix="atomic_",
        dir=os.path.dirname(path) or ".",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        shutil.move(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def cleanup_stale_pid_file(path: str) -> bool:
    """检查 PID 文件的 boot_token 是否来自当前启动。
    如果来自上一次启动（boot_token > current）→ 删除文件 → 返回 True。
    如果来自当前启动或旧格式 → 不碰 → 返回 False。
    """
    if not os.path.exists(path):
        return False
    pid, saved_token = read_pid_with_boot_token(path)
    if pid is None:
        # 文件损坏 → 删掉
        try:
            os.remove(path)
        except Exception:
            pass
        return True
    if saved_token is None:
        # 旧格式（纯 PID），不删，让调用方用自己的方式判断
        return False
    current_token = get_boot_token()
    if saved_token > current_token:
        # 上次启动的 token 不可能 > 当前 token（GetTickCount64 重启归零）
        try:
            os.remove(path)
        except Exception:
            pass
        return True
    return False
