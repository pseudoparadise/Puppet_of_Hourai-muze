"""
polling_loop.py — DSphantom 轮询守护进程（修复版）
每 5 分钟执行一次 bark_trigger.main()，持续运行。
ADD: 启动/停止/心跳日志，time-driven diary，深渊审计定时器
用法：python polling_loop.py
"""
import sys
import os
import time
import traceback
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

INTERVAL_MINUTES = 5
HEARTBEAT_INTERVAL = 30 * 60  # 心跳间隔：30分钟
DIARY_INTERVAL = 24 * 3600     # 日记最小间隔：24小时
AUDIT_INTERVAL = 6 * 3600      # 审计间隔：6小时


def _log_event(event_type: str, extra: dict = None):
    """向 trigger.log 写入一条事件日志。"""
    from delegate_tools import now_utc, fmt_time
    log_path = os.path.join(PROJECT_ROOT, "trigger.log")
    entry = {"timestamp": fmt_time(now_utc()), "event": event_type}
    if extra:
        entry.update(extra)
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[_log_event 写入失败] {e}", file=sys.stderr)


def main():
    print(f"[DSphantom轮询守护] 启动，每 {INTERVAL_MINUTES} 分钟检测一次沉默状态")
    print(f"[DSphantom轮询守护] 当前目录: {os.getcwd()}")
    print(f"[DSphantom轮询守护] 按 Ctrl+C 停止\n")

    from bark_trigger import main as bark_main
    from delegate.dreaming import chain_dream, weekly_sweep

    # ── 毒点15修复：启动日志 ──
    _log_event("polling_start")

    # ── 长期优化四：首次启动立即执行一次审计 ──
    try:
        from memory.memory_manager import run_audit
        _log_event("audit_start")
        run_audit()
        _log_event("audit_complete", {"result": "initial"})
    except Exception as e:
        _log_event("audit_error", {"error": str(e)})

    # ── 追补日记：启动时当天日记不存在则立即生成（统一调度，避免 bark_trigger 双重触发） ──
    from delegate_tools import now_utc as _now_utc_startup
    today_str = _now_utc_startup().strftime("%Y-%m-%d")
    diary_check = os.path.join(PROJECT_ROOT, "diary", f"{today_str}.md")
    if not os.path.exists(diary_check):
        print(f"[每日日记] 启动追补：{today_str} 日记不存在，立即生成...")
        _log_event("diary_scheduled", {"reason": "startup_catchup"})
        try:
            chain_dream()
        except Exception as e:
            _log_event("diary_error", {"error": str(e)[:200]})

    # ── 计时器 ──
    last_heartbeat = time.time()
    last_diary_time = time.time()  # 毒点12：时间驱动日记
    last_audit_time = time.time()  # 长期优化四
    last_miner_time = time.time()  # 长期优化五：人格蒸馏
    last_sweep_time = time.time()  # 周收拢

    cycle_count = 0
    consecutive_empty = 0

    while True:
        cycle_count += 1

        try:
            print(f"\n{'='*40}")
            bark_main()
            consecutive_empty = 0
        except KeyboardInterrupt:
            print("\n[DSphantom轮询守护] 已停止。")
            _log_event("polling_stop", {"reason": "user_interrupt"})
            break
        except Exception as e:
            print(f"[DSphantom轮询守护] 异常: {e}")
            traceback.print_exc()
            _log_event("polling_crash", {"error": str(e)[:200]})

        now_ts = time.time()

        # ── 毒点17修复：心跳日志（每30分钟） ──
        if now_ts - last_heartbeat > HEARTBEAT_INTERVAL:
            _log_event("polling_heartbeat", {"cycles_since_last": cycle_count})
            last_heartbeat = now_ts
            cycle_count = 0

        # ── 毒点12修复：时间驱动日记（每24小时） ──
        if now_ts - last_diary_time > DIARY_INTERVAL:
            print("[每日日记] 时间驱动生成...")
            _log_event("diary_scheduled")
            try:
                chain_dream()
            except Exception as e:
                _log_event("diary_error", {"error": str(e)[:200]})
            last_diary_time = now_ts

        # ── 长期优化四：深渊审计（每6小时） ──
        if now_ts - last_audit_time > AUDIT_INTERVAL:
            print("[深渊审计] 定时执行...")
            try:
                from memory.memory_manager import run_audit
                _log_event("audit_start")
                run_audit()
                _log_event("audit_complete", {"result": "scheduled"})
            except Exception as e:
                _log_event("audit_error", {"error": str(e)[:200]})
            last_audit_time = now_ts

        # ── 长期优化五：人格蒸馏自动调度（每24小时，在日记之后） ──
        if now_ts - last_miner_time > DIARY_INTERVAL:
            print("[人格蒸馏] 自动调度中...")
            _log_event("miner_scheduled")
            try:
                from persona.miner import main as miner_main
                miner_main()
            except Exception as e:
                _log_event("miner_error", {"error": str(e)[:200]})
            last_miner_time = now_ts

        # ── 周收拢：每7天聚合待办事项 ──
        SWEEP_INTERVAL = 7 * 24 * 3600
        if now_ts - last_sweep_time > SWEEP_INTERVAL:
            print("[周收拢] 聚合近7天待办...")
            _log_event("sweep_scheduled")
            try:
                weekly_sweep()
            except Exception as e:
                _log_event("sweep_error", {"error": str(e)[:200]})
            last_sweep_time = now_ts

        # 倒计时
        for i in range(INTERVAL_MINUTES * 60, 0, -1):
            try:
                time.sleep(1)
            except KeyboardInterrupt:
                print("\n[DSphantom轮询守护] 已停止。")
                _log_event("polling_stop", {"reason": "user_interrupt"})
                return
        print()


if __name__ == "__main__":
    main()