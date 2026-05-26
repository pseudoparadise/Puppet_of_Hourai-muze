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

    # PID 锁
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as f:
                old_pid = int(f.read().strip())
            import ctypes
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED, False, old_pid)
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                print(f"[daemon] 已有守护进程运行 (PID {old_pid})")
                sys.exit(1)
        except Exception:
            pass
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

    # 主循环：启动 + 监护 polling_loop
    RESTART_DELAY = 10
    try:
        while True:
            print(f"[daemon] 启动轮询守护...")
            polling_proc = subprocess.Popen(
                [PYTHON, os.path.join(PROJECT_ROOT, "polling_loop.py")],
                cwd=PROJECT_ROOT,
            )
            while True:
                try:
                    ret = polling_proc.wait(timeout=5)
                    if ret == 0:
                        print(f"[daemon] 轮询守护正常退出。")
                        cleanup()
                        return
                    else:
                        print(f"[daemon] 轮询守护异常退出 (code={ret})，{RESTART_DELAY}s 后重启...")
                        time.sleep(RESTART_DELAY)
                        break
                except subprocess.TimeoutExpired:
                    # 还在跑，检查音乐轮询是否活着
                    if music_proc and music_proc.poll() is not None:
                        print("[daemon] 音乐轮询已退出，重新拉起...")
                        music_proc = start_music_poll()
    except KeyboardInterrupt:
        cleanup()


if __name__ == "__main__":
    main()
