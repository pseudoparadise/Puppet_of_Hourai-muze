import ctypes
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

from boot_guard import get_boot_token, cleanup_stale_pid_file, read_pid_with_boot_token
from clock import BJT
from .panic import panic_popup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_pid_alive(pid: int) -> bool:
    SYNCHRONIZE = 0x00100000
    PROCESS_QUERY_INFORMATION = 0x0400
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_INFORMATION, False, pid)
    if not h:
        return False
    exit_code = ctypes.c_ulong()
    alive = False
    if kernel32.GetExitCodeProcess(h, ctypes.byref(exit_code)):
        alive = exit_code.value == STILL_ACTIVE
    kernel32.CloseHandle(h)
    return alive


def _describe_process(line: str) -> tuple[str | None, str]:
    """从 wmic csv 行中提取 PID 和可读摘要。返回 (pid_str, summary)。"""
    parts = line.split(',')
    pid_str = parts[-1].strip() if len(parts) >= 3 else ""
    cmd = parts[-2].strip() if len(parts) >= 3 else line[:120]
    return (pid_str if pid_str.isdigit() else None, cmd[:150])


def _enumerate_python_processes() -> list[tuple[str, str]]:
    """枚举所有 Python 进程，返回 [(pid_str, command_line), ...]。
    全量扫描 wmic 进程列表，Python 自己过滤——wmic where 语法在某些 Windows 上静默失败。"""
    try:
        result = subprocess.run(
            ['wmic', 'process', 'get', 'ProcessId,CommandLine', '/format:csv'],
            capture_output=True, timeout=10
        )
        text = result.stdout.decode('gbk', errors='replace')
        procs = []
        for line in text.split('\n'):
            if 'python' not in line.lower():
                continue
            pid_str, cmd = _describe_process(line)
            if pid_str:
                procs.append((pid_str, cmd))
        return procs
    except Exception as e:
        print(f"[daemon] preflight: 进程枚举失败: {e}", file=sys.stderr)
        import traceback as _tb_enum
        _tb_enum.print_exc()
        return []


def preflight_cleanup():
    current_pid = os.getpid()
    killed = 0
    total_found = 0

    print("[daemon] preflight: 扫描所有 Python 进程...")
    all_python = _enumerate_python_processes()

    if not all_python:
        print("[daemon] preflight: *** 警告 *** 未能枚举任何 Python 进程！wmic 可能不可用。", file=sys.stderr)
        return

    for pid_str, cmd in all_python:
        lower = cmd.lower()
        is_gt = any(x in lower for x in ['daemon.py', 'music_poll', 'bark_trigger',
                                           'console.py', 'trigger.py', 'preflight.py',
                                           'polling_loop', 'music_sync'])

        if any(x in lower for x in ['daemon.py', 'music_poll', 'bark_trigger']):
            total_found += 1
            pid_int = int(pid_str)

            # 安全检查：绝对不能杀非 ghost-trigger 进程
            if not any(x in cmd for x in ['daemon.py', 'music_poll', 'bark_trigger', 'music_poll.py', 'daemon.py', 'bark_trigger.py']):
                print(f"[daemon] preflight: 跳过 PID {pid_str} — 命令不匹配 ghost-trigger 特征 ({cmd[:80]})")
                continue

            if pid_int != current_pid:
                try:
                    k = ctypes.windll.kernel32
                    h = k.OpenProcess(0x0001, False, pid_int)
                    if h:
                        k.TerminateProcess(h, 0)
                        k.CloseHandle(h)
                        killed += 1
                        print(f"[daemon] preflight: 清除孤儿 {pid_str} ({cmd[:80]})")
                except Exception as exc:
                    print(f"[daemon] preflight: 清除 PID {pid_str} 失败: {exc}", file=sys.stderr)

    print(f"[daemon] preflight: 系统中共 {len(all_python)} 个 Python 进程")
    for pid_str, cmd in all_python:
        marker = "← 己" if int(pid_str) == current_pid else ""
        is_gt = any(x in cmd.lower() for x in ['daemon.py', 'music_poll', 'bark_trigger',
                                                 'console.py', 'trigger.py', 'preflight.py',
                                                 'polling_loop', 'music_sync'])
        tag = "[ghost-trigger]" if is_gt else "[外部]"
        print(f"[daemon] preflight:   PID {pid_str:>6} {tag} {cmd[:110]} {marker}")

    if killed:
        print(f"[daemon] preflight: 共清除 {killed} 个孤儿进程")
        time.sleep(1.5)

    EXPLOSION_THRESHOLD = 5
    if total_found > EXPLOSION_THRESHOLD:
        msg = (f"preflight 检测到 {total_found} 个 ghost-trigger 进程！\n"
               f"判定为级联爆炸，已全部清除。\n"
               f"额外冷却 30s 后继续。")
        print(f"[daemon] CRITICAL: {msg}")
        panic_popup("进程级联爆炸", f"发现 {total_found} 个 ghost-trigger 进程\n阈值: {EXPLOSION_THRESHOLD}\n已全部清除，冷却 30s")
        time.sleep(30)

    for fname in ['.daemon_state.json', '.music_state.json',
                  '.polling_loop.pid', '.music_sync.pid', '.console.pid']:
        path = os.path.join(PROJECT_ROOT, fname)
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"[daemon] preflight: 清理残留文件 {fname}")
            except Exception:
                pass


def startup_cleanup(boot_token: int):
    print("[daemon] 执行启动清理...")

    BOOT_CACHE = os.path.join(PROJECT_ROOT, ".boot_cache")
    try:
        with open(BOOT_CACHE, "w") as f:
            f.write(str(boot_token))
    except Exception:
        pass

    music_ctx = os.path.join(PROJECT_ROOT, ".music_context.txt")
    if os.path.exists(music_ctx):
        try:
            os.remove(music_ctx)
            print("[daemon] 清理残留的 .music_context.txt")
        except Exception:
            pass

    session_state = os.path.join(PROJECT_ROOT, ".session_state.json")
    if os.path.exists(session_state):
        try:
            import json as _json_ss
            with open(session_state, "r", encoding="utf-8") as f:
                ss = _json_ss.load(f)
            saved_boot = ss.get("boot_token", None)
            current_boot = boot_token
            if saved_boot is not None and saved_boot != current_boot:
                os.remove(session_state)
                print("[daemon] 检测到系统重启，清除旧会话状态")
        except Exception:
            pass

    for pid_name in [".polling_loop.pid", ".music_sync.pid"]:
        pid_path = os.path.join(PROJECT_ROOT, pid_name)
        if cleanup_stale_pid_file(pid_path):
            print(f"[daemon] 清理旧启动残留: {pid_name}")
