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

    # ── 追补日记：启动时检查昨天（北京时间）日记是否存在 ──
    from datetime import datetime as _dt_startup, timezone as _tz_startup, timedelta as _td_startup
    _bj_now_startup = _dt_startup.now(_tz_startup.utc) + _td_startup(hours=8)
    _yesterday_bj = (_bj_now_startup - _td_startup(days=1)).strftime("%Y-%m-%d")
    diary_check = os.path.join(PROJECT_ROOT, "diary", f"{_yesterday_bj}.md")
    if not os.path.exists(diary_check):
        print(f"[每日日记] 启动追补：{_yesterday_bj} 日记不存在，立即生成...")
        _log_event("diary_scheduled", {"reason": "startup_catchup", "date": _yesterday_bj})
        try:
            chain_dream(_yesterday_bj)
        except Exception as e:
            _log_event("diary_error", {"error": str(e)[:200]})

    # ── 待办提醒：扫描 todo/commitments 卡，定时推送 ──
    REMINDED_PATH = os.path.join(PROJECT_ROOT, "memory", "reminded_todos.json")
    REMINDER_COOLDOWN = 60  # 同一张卡至少间隔 60 分钟再提醒

    def _check_todo_reminders(now_local):
        """扫描到期待办，Bark 推送提醒。返回提醒数量。"""
        import sqlite3, json as _json
        from datetime import datetime as _dt, timedelta as _td
        db_path = os.path.join(PROJECT_ROOT, "memory", "cards.db")
        if not os.path.exists(db_path):
            return 0
        # 加载已提醒记录
        reminded = {}
        if os.path.exists(REMINDED_PATH):
            try:
                with open(REMINDED_PATH, "r", encoding="utf-8") as _rf:
                    reminded = _json.load(_rf)
            except Exception:
                pass
        today_str = now_local.strftime("%Y-%m-%d")
        now_minutes = now_local.hour * 60 + now_local.minute
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(
            "SELECT id, title, content, target_date, created_at FROM cards "
            "WHERE review_status='final' AND resolved=0 "
            "AND category IN ('todo','commitments','daily_life') "
            "ORDER BY created_at DESC LIMIT 20"
        )
        pushed = 0
        for row in c.fetchall():
            cid = row['id']
            ctitle = row['title']
            tdate = row['target_date'] or ""
            # 跳过已提醒且未过冷却的
            if cid in reminded:
                last_ts = _dt.fromisoformat(reminded[cid])
                if (now_local - last_ts).total_seconds() < REMINDER_COOLDOWN * 60:
                    continue
            # 判断是否该提醒
            should_remind = False
            remind_reason = ""
            # 今天到期的带时间锚点
            if tdate.startswith(today_str) and " " in tdate:
                try:
                    target_time = _dt.fromisoformat(tdate)
                    target_minutes = target_time.hour * 60 + target_time.minute
                    if 0 <= target_minutes - now_minutes <= 30:
                        should_remind = True
                        remind_reason = f"{target_time.strftime('%H:%M')} 到期"
                except Exception:
                    pass
            # 今天到期的纯日期（全天待办，早9点提醒）
            elif tdate == today_str:
                if 8 <= now_local.hour <= 10:
                    should_remind = True
                    remind_reason = f"今天待办"
            # 即将到期（明天）
            elif tdate:
                try:
                    target_dt = _dt.fromisoformat(tdate)
                    if (target_dt - now_local).days <= 1 and now_local.hour >= 20:
                        should_remind = True
                        remind_reason = f"明天 {target_dt.strftime('%H:%M')} 到期"
                except Exception:
                    pass
            if not should_remind:
                continue
            # 发送 Bark 推送
            bark_key = config["global"].get("bark_device_key", "")
            if bark_key and bark_key != "你的BarkKey填这里":
                import requests as _req
                msg = f"⏰ {ctitle} — {remind_reason}"
                try:
                    _req.get(f"https://api.day.app/{bark_key}/{msg}", timeout=10)
                    pushed += 1
                    reminded[cid] = now_local.isoformat()
                    print(f"[待办提醒] {ctitle} — {remind_reason}")
                    _log_event("todo_reminder", {"card_id": cid, "title": ctitle[:40]})
                except Exception as e:
                    print(f"[待办提醒] 推送失败: {e}")
        conn.close()
        # 清理过期提醒记录
        reminded = {k: v for k, v in reminded.items()
                    if (now_local - _dt.fromisoformat(v)).total_seconds() < REMINDER_COOLDOWN * 60 * 2}
        try:
            from delegate_tools import atomic_write_json as _awj_rem
            _awj_rem(REMINDED_PATH, reminded)
        except Exception:
            pass
        return pushed

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
        now_ts = time.time()

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

        # ── 毒点17修复：心跳日志（每30分钟） ──
        if now_ts - last_heartbeat > HEARTBEAT_INTERVAL:
            _log_event("polling_heartbeat", {"cycles_since_last": cycle_count})
            last_heartbeat = now_ts
            cycle_count = 0

        # ── 毒点12修复v2：日记对齐到自然日 00:00-00:30（北京时间） ──
        from datetime import datetime as _dt_now
        bj_now = _dt_now.utcnow().astimezone(__import__('datetime', fromlist=['timezone']).timezone(
            __import__('datetime', fromlist=['timedelta']).timedelta(hours=8)))
        bj_hour = bj_now.hour
        bj_minute = bj_now.minute
        # 在北京时间 00:00-00:30 窗口内触发，且上次触发不是今天
        today_bj = bj_now.strftime("%Y-%m-%d")
        last_diary_date = getattr(main, '_last_diary_date', "")
        if bj_hour == 0 and bj_minute < 30 and last_diary_date != today_bj:
            from datetime import datetime as _dt_mid, timezone as _tz_mid, timedelta as _td_mid
            _bj_mid = _dt_mid.now(_tz_mid.utc) + _td_mid(hours=8)
            _yesterday_mid = (_bj_mid - _td_mid(days=1)).strftime("%Y-%m-%d")
            print(f"[每日日记] 自然日触发 — 北京时间 {bj_now.strftime('%H:%M')}，生成 {_yesterday_mid} 日记")
            _log_event("diary_scheduled", {"reason": "midnight_window", "date": _yesterday_mid})
            try:
                chain_dream(_yesterday_mid)
                main._last_diary_date = today_bj  # type: ignore
            except Exception as e:
                _log_event("diary_error", {"error": str(e)[:200]})
                # 失败不更新 _last_diary_date，让后续周期重试

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

        # ── 长期优化五v2：人格蒸馏对齐日记，日记生成后触发（00:30-01:00窗口） ──
        if bj_hour == 0 and 30 <= bj_minute < 60 and last_diary_date != getattr(main, '_last_miner_date', ""):
            print("[人格蒸馏] 日界线后触发 — 在日记生成之后")
            _log_event("miner_scheduled", {"reason": "after_diary"})
            try:
                from persona.miner import main as miner_main
                miner_main()
                main._last_miner_date = today_bj  # type: ignore
            except Exception as e:
                _log_event("miner_error", {"error": str(e)[:200]})

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

        # ── 待办提醒扫描 ──
        from datetime import datetime as _dt_remind, timezone as _tz_remind, timedelta as _td_remind
        _bj_remind = _dt_remind.now(_tz_remind.utc) + _td_remind(hours=8)
        _check_todo_reminders(_bj_remind)

        # 倒计时：消除累积漂移，精确 5 分钟间隔
        elapsed = time.time() - now_ts
        remaining = max(0, INTERVAL_MINUTES * 60 - elapsed)
        try:
            time.sleep(remaining)
        except KeyboardInterrupt:
            print("\n[DSphantom轮询守护] 已停止。")
            _log_event("polling_stop", {"reason": "user_interrupt"})
            return
        print()


if __name__ == "__main__":
    main()