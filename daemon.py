"""
daemon.py — DSphantom 进程管家（唯一入口）
  python daemon.py              # 启动全部服务
  python daemon.py --no-music   # 只跑轮询守护，不启音乐同步
用法替代 start_daemon.bat。
"""
import sys
import os
import time
import json
import signal
import subprocess
import socket

from boot_guard import (
    get_boot_token,
    write_pid_with_boot_token,
    read_pid_with_boot_token,
    cleanup_stale_pid_file,
)

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

MUSIC_POLL_SCRIPT = "music_poll.py"
PID_FILE = os.path.join(PROJECT_ROOT, ".daemon.pid")


def _port_in_use(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        result = s.connect_ex(("127.0.0.1", port))
        return result == 0
    except Exception:
        return False
    finally:
        s.close()


def _is_pid_alive(pid: int) -> bool:
    """检测指定 PID 进程是否还在运行（Windows）。"""
    import ctypes
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


def _is_service_running(pid_file: str) -> bool:
    """检查 pid_file 是否有效且对应进程还活着。
    - 先从旧启动残留清理（boot_token 比对）
    - 再检查 PID 是否存活
    - 活着返回 True，死了清理残留文件返回 False。"""
    if not os.path.exists(pid_file):
        return False
    try:
        pid, boot_token = read_pid_with_boot_token(pid_file)
        if pid is None:
            os.remove(pid_file)
            return False
        # 来自上一次启动 → 无条件清理
        if boot_token is not None and boot_token > get_boot_token():
            print(f"[daemon] {os.path.basename(pid_file)} 来自上一次启动，清理")
            os.remove(pid_file)
            return False
        # 同一次启动 → 检查进程存活
        if _is_pid_alive(pid):
            return True
        os.remove(pid_file)
    except Exception:
        pass
    return False


def _write_pid(path: str):
    write_pid_with_boot_token(path, os.getpid())


def _remove_pid(path: str):
    try:
        os.remove(path)
    except Exception:
        pass


def start_music_poll():
    p = subprocess.Popen(
        [PYTHON, os.path.join(PROJECT_ROOT, MUSIC_POLL_SCRIPT)],
        cwd=PROJECT_ROOT,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    print(f"[daemon] 音乐轮询已启动 (PID {p.pid})")
    return p


def stop_music_poll(proc):
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        print(f"[daemon] 音乐轮询已停止")



def _startup_cleanup():
    """启动清理：移除上一次启动的残留文件，写入新的 boot 缓存。"""
    print("[daemon] 执行启动清理...")

    # 更新 boot 缓存（供 bark_trigger 检测重启）
    BOOT_CACHE = os.path.join(PROJECT_ROOT, ".boot_cache")
    try:
        with open(BOOT_CACHE, "w") as f:
            f.write(str(get_boot_token()))
    except Exception:
        pass

    # 清理音乐上下文（跨启动无效）
    music_ctx = os.path.join(PROJECT_ROOT, ".music_context.txt")
    if os.path.exists(music_ctx):
        try:
            os.remove(music_ctx)
            print("[daemon] 清理残留的 .music_context.txt")
        except Exception:
            pass

    # 检测重启 → 丢弃旧 session state
    session_state = os.path.join(PROJECT_ROOT, ".session_state.json")
    if os.path.exists(session_state):
        try:
            with open(session_state, "r", encoding="utf-8") as f:
                ss = json.load(f)
            saved_boot = ss.get("boot_token", None)
            current_boot = get_boot_token()
            if saved_boot is not None and saved_boot != current_boot:
                os.remove(session_state)
                print("[daemon] 检测到系统重启，清除旧会话状态")
        except Exception:
            pass

    # 清理其他 PID 文件的旧启动残留
    for pid_name in [".polling_loop.pid", ".music_sync.pid"]:
        pid_path = os.path.join(PROJECT_ROOT, pid_name)
        if cleanup_stale_pid_file(pid_path):
            print(f"[daemon] 清理旧启动残留: {pid_name}")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-music", action="store_true", help="不启动音乐同步服务")
    args = parser.parse_args()

    # ── PID 锁：检测旧实例是否存活 ──
    if _is_service_running(PID_FILE):
        pid, _ = read_pid_with_boot_token(PID_FILE)
        print(f"[daemon] 已有守护进程运行 (PID {pid or '?'})")
        sys.exit(1)
    _write_pid(PID_FILE)
    _startup_cleanup()

    # ── 清理旧 polling_loop.pid（console.py 现在接管轮询）──
    old_poll_pid = os.path.join(PROJECT_ROOT, ".polling_loop.pid")
    if os.path.exists(old_poll_pid):
        try:
            cleanup_stale_pid_file(old_poll_pid)
            if os.path.exists(old_poll_pid):
                os.remove(old_poll_pid)
        except Exception:
            pass

    print("DSphantom 守护进程启动 (精简版: 仅音乐)")
    print(f"  PID: {os.getpid()}")
    print(f"  项目: {PROJECT_ROOT}")
    print(f"  提示: 轮询守护已迁移至 console.py 内置")

    music_proc = None
    if not args.no_music:
        music_proc = start_music_poll()

    def cleanup():
        print("\n[daemon] 正在停止所有服务...")
        if music_proc and music_proc.poll() is None:
            music_proc.terminate()
            try:
                music_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                music_proc.kill()
        _remove_pid(PID_FILE)
        print("[daemon] 已停止。")

    # 信号处理
    def _on_signal(sig, frame):
        cleanup()
        sys.exit(0)

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    MUSIC_STALE_SEC = 120
    MUSIC_MAX_CRASHES = 5       # 5分钟内最多崩溃5次
    MUSIC_CRASH_WINDOW = 300    # 5分钟窗口
    MUSIC_BASE_DELAY = 10       # 基础重启延迟
    music_crash_times = []      # 崩溃时间戳列表

    # ── 主循环：仅监护 music_poll ──
    try:
        while True:
            # ── 监护 music_poll ──
            if music_proc:
                need_restart = False
                restart_reason = ""
                if music_proc.poll() is not None:
                    need_restart = True
                    restart_reason = "进程已退出"
                else:
                    state_path = os.path.join(PROJECT_ROOT, ".music_state.json")
                    try:
                        age = time.time() - os.path.getmtime(state_path)
                        if age > MUSIC_STALE_SEC:
                            need_restart = True
                            restart_reason = f"心跳超时({int(age)}s)"
                            music_proc.kill()
                            try:
                                music_proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                pass
                    except FileNotFoundError:
                        if music_proc.poll() is not None:
                            need_restart = True
                            restart_reason = "状态文件缺失且进程已死"
                    except Exception:
                        pass

                if need_restart:
                    # ── 崩溃保护：滑动窗口计数 ──
                    now_ts = time.time()
                    music_crash_times = [t for t in music_crash_times if now_ts - t < MUSIC_CRASH_WINDOW]
                    music_crash_times.append(now_ts)
                    crash_count = len(music_crash_times)

                    if crash_count > MUSIC_MAX_CRASHES:
                        print(f"[daemon] 音乐轮询 {MUSIC_CRASH_WINDOW}s 内崩溃 {crash_count} 次，停止重试！")
                        print(f"[daemon] 请手动检查 music_poll.py 状态，daemon 继续运行但不再重拉音乐服务")
                        # 清空崩溃记录，等手动修复后会恢复
                        while True:
                            time.sleep(60)
                            if music_proc is None or music_proc.poll() is not None:
                                continue
                            # 检查是否手动恢复了
                            try:
                                if os.path.exists(os.path.join(PROJECT_ROOT, ".music_state.json")):
                                    age = time.time() - os.path.getmtime(os.path.join(PROJECT_ROOT, ".music_state.json"))
                                    if age < MUSIC_STALE_SEC:
                                        music_crash_times = []
                                        print("[daemon] 检测到音乐服务手动恢复，重置崩溃计数")
                                        break
                            except Exception:
                                pass

                    # 指数退避: 10s → 20s → 40s → ... → max 300s
                    delay = min(MUSIC_BASE_DELAY * (2 ** (crash_count - 1)), 300)
                    print(f"[daemon] 音乐轮询 {restart_reason}，{delay}s 后重拉 (崩溃#{crash_count})...")
                    time.sleep(delay)
                    music_proc = start_music_poll()
                else:
                    time.sleep(5)  # 正常轮询间隔
            else:
                # music_proc 为 None 且 --no-music → 纯空闲
                print("[daemon] 音乐服务已禁用 (--no-music)，守护空闲中...")
                time.sleep(60)
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    from crash_reporter import install
    install()
    main()
