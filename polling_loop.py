"""
polling_loop.py — DSphantom 轮询守护进程
每 5 分钟执行一次 bark_trigger.main()，持续运行。
用法：python polling_loop.py
"""
import sys
import os
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

INTERVAL_MINUTES = 5  # 轮询间隔

def main():
    print(f"[DSphantom轮询守护] 启动，每 {INTERVAL_MINUTES} 分钟检测一次沉默状态")
    print(f"[DSphantom轮询守护] 当前目录: {os.getcwd()}")
    print(f"[DSphantom轮询守护] 按 Ctrl+C 停止\n")

    from bark_trigger import main as bark_main

    while True:
        try:
            print(f"\n{'='*40}")
            bark_main()
        except KeyboardInterrupt:
            print("\n[DSphantom轮询守护] 已停止。")
            break
        except Exception as e:
            print(f"[DSphantom轮询守护] 异常: {e}")
            traceback.print_exc()

        # 倒计时
        for i in range(INTERVAL_MINUTES * 60, 0, -1):
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                print("\n[DSphantom轮询守护] 已停止。")
                return
        print()  # 换行，准备下一轮

if __name__ == "__main__":
    main()
