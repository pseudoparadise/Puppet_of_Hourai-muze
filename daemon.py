"""
daemon.py — DSphantom 进程管家（唯一入口）
  python daemon.py              # 启动全部服务
  python daemon.py --no-music   # 只跑轮询守护，不启音乐同步
用法替代 start_daemon.bat。
"""
import sys
import os
import time
import signal
import subprocess
import socket

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable

MUSIC_POLL_SCRIPT = "music_poll.py"
POLLING_SCRIPT = "polling_loop.py"
PID_FILE = os.path.join(PROJECT_ROOT, ".daemon.pid")

if not os.path.exists(os.path.join(PROJECT_ROOT, POLLING_SCRIPT)):
    print(f"[daemon] 致命错误：找不到 {POLLING_SCRIPT}")
    print(f"  PROJECT_ROOT = {PROJECT_ROOT}")
    print(f"  cwd = {os.getcwd()}")
    print(f"  请从项目根目录启动 daemon.py，或使用 start_daemon.bat")
    sys.exit(1)


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
    """检查 pid_file 是否存在且对应进程还活着。活着返回 True，死了清理残留文件返回 False。"""
    if not os.path.exists(pid_file):
        return False
    try:
        with open(pid_file, "r") as f:
            pid = int(f.read().strip())
        if _is_pid_alive(pid):
            return True
        os.remove(pid_file)
    except Exception:
        pass
    return False


def _write_pid(path: str):
    with open(path, "w") as f:
        f.write(str(os.getpid()))


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



def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-music", action="store_true", help="不启动音乐同步服务")
    args = parser.parse_args()

    # ── PID 锁：检测旧实例是否存活 ──
    if _is_service_running(PID_FILE):
        with open(PID_FILE, "r") as f:
            old_pid = f.read().strip()
        print(f"[daemon] 已有守护进程运行 (PID {old_pid})")
        sys.exit(1)
    _write_pid(PID_FILE)

    print("DSphantom 守护进程启动")
    print(f"  PID: {os.getpid()}")
    print(f"  项目: {PROJECT_ROOT}")

    music_proc = None
    if not args.no_music:
        music_proc = start_music_poll()

    polling_proc = None

    def cleanup():
        print("\n[daemon] 正在停止所有服务...")
        if polling_proc and polling_proc.poll() is None:
            polling_proc.terminate()
            try:
                polling_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                polling_proc.kill()
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
    POLLING_PID_FILE = os.path.join(PROJECT_ROOT, ".polling_loop.pid")
    RESTART_DELAY = 10

    # ── 预检：polling_loop 是否已在运行（一个服务活着不阻其他服务）──
    polling_skip = False
    if _is_service_running(POLLING_PID_FILE):
        with open(POLLING_PID_FILE, "r") as f:
            print(f"[daemon] polling_loop 已在运行 (PID {f.read().strip()})，跳过启动")
        polling_skip = True

    # ── 主循环：启动 + 监护子服务 ──
    try:
        while True:
            if not polling_skip:
                print(f"[daemon] 启动轮询守护...")
                polling_proc = subprocess.Popen(
                    [PYTHON, os.path.join(PROJECT_ROOT, POLLING_SCRIPT)],
                    cwd=PROJECT_ROOT,
                )
            else:
                polling_proc = None

            while True:
                # ── 监护 polling_loop ──
                if polling_proc is not None:
                    try:
                        ret = polling_proc.wait(timeout=5)
                        if ret == 0:
                            if _is_service_running(POLLING_PID_FILE):
                                # 已有另一个实例接替（或被我们 kill 后有残留）→ 跳过
                                print(f"[daemon] polling_loop 退出但已有其他实例运行，跳过重启")
                                polling_skip = True
                                polling_proc = None
                            else:
                                print(f"[daemon] polling_loop 退出 (code=0)，{RESTART_DELAY}s 后重拉...")
                                time.sleep(RESTART_DELAY)
                                break
                        else:
                            print(f"[daemon] polling_loop 异常退出 (code={ret})，{RESTART_DELAY}s 后重启...")
                            time.sleep(RESTART_DELAY)
                            break
                    except subprocess.TimeoutExpired:
                        pass  # 还活着，继续监护
                elif not polling_skip:
                    # polling_proc 为 None 且未跳过 → 跳出内循环重拉
                    break
                elif _is_service_running(POLLING_PID_FILE):
                    # 跳过模式：定期确认外部实例还活着
                    pass
                else:
                    # 外部实例已死 → 恢复接管
                    print("[daemon] 外部 polling_loop 已退出，恢复接管")
                    polling_skip = False
                    break

                # ── 监护 music_poll ──
                if music_proc:
                    need_restart = False
                    if music_proc.poll() is not None:
                        need_restart = True
                        print("[daemon] 音乐轮询进程已退出，重新拉起...")
                    else:
                        state_path = os.path.join(PROJECT_ROOT, ".music_state.json")
                        try:
                            age = time.time() - os.path.getmtime(state_path)
                            if age > MUSIC_STALE_SEC:
                                need_restart = True
                                print(f"[daemon] 音乐轮询心跳超时 ({int(age)}s 未更新)，强制重拉...")
                                music_proc.kill()
                                try:
                                    music_proc.wait(timeout=3)
                                except subprocess.TimeoutExpired:
                                    pass
                        except FileNotFoundError:
                            need_restart = True
                            print("[daemon] .music_state.json 不存在，重拉音乐轮询...")
                        except Exception:
                            pass
                    if need_restart:
                        music_proc = start_music_poll()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    from crash_reporter import install
    install()
    main()
