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
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

from clock import beijing_now, beijing_today, beijing_yesterday

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


def _ensure_diary(chain_dream, reason: str = "scheduled"):
    """确保昨天（北京时间）的日记已生成。无文件则调用 chain_dream。"""
    yesterday = beijing_yesterday()
    diary_path = os.path.join(PROJECT_ROOT, "diary", f"{yesterday}.md")
    if not os.path.exists(diary_path):
        print(f"[每日日记] {reason} — {yesterday} 日记不存在，立即生成...")
        _log_event("diary_scheduled", {"reason": reason, "date": yesterday})
        try:
            chain_dream(yesterday)
        except Exception as e:
            _log_event("diary_error", {"error": str(e)[:200]})


def _ensure_miner():
    """确保今天（北京时间）已执行人格蒸馏。"""
    miner_state_path = os.path.join(PROJECT_ROOT, "persona", "miner_state.json")
    today_str = beijing_today()
    last_date = ""
    if os.path.exists(miner_state_path):
        try:
            with open(miner_state_path, "r", encoding="utf-8") as f:
                last_date = json.load(f).get("last_analysis_date", "")
        except Exception:
            pass
    if last_date != today_str:
        print(f"[礦工] 上次蒸馏 {last_date or '无'}，触发蒸馏...")
        _log_event("miner_scheduled", {"reason": "daily", "last_date": last_date})
        try:
            from persona.miner import main as miner_main
            miner_main()
        except Exception as e:
            _log_event("miner_error", {"error": str(e)[:200]})


def _sync_supabase_count():
    """查询 Supabase 今日录入次数，写入今日日记。"""
    config_path = os.path.join(PROJECT_ROOT, "config.json")
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except Exception:
        return

    supabase_url = config.get("global", {}).get("supabase_url", "")
    supabase_key = config.get("global", {}).get("supabase_key", "")
    if not supabase_url or not supabase_key or supabase_key == "你的SupabaseKey填这里":
        return

    # 北京时间今日范围 → UTC（Supabase 存 UTC，北京时间领先 8h）
    from datetime import timezone as _tz, timedelta as _td
    BJT = _tz(_td(hours=8))
    bj_now = datetime.now(_tz.utc).astimezone(BJT)
    bj_start = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
    bj_end = bj_now.replace(hour=23, minute=59, second=59, microsecond=999999)
    utc_start = bj_start.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")
    utc_end = bj_end.astimezone(_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")

    count = 0
    try:
        import requests as _req
        headers = {"apikey": supabase_key, "Authorization": f"Bearer {supabase_key}"}
        # 用 gt/lt 范围过滤，Supabase REST 的时区过滤器格式
        url = (f"{supabase_url}/rest/v1/app_usage_logs"
               f"?select=recorded_at"
               f"&recorded_at=gte.{utc_start}"
               f"&recorded_at=lte.{utc_end}")
        r = _req.get(url, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            count = len(data) if isinstance(data, list) else 0
    except Exception as e:
        print(f"[Supabase计数] 查询失败: {e}")
        return

    # 写入今日日记
    today_str = bj_now.strftime("%Y-%m-%d")
    diary_path = os.path.join(PROJECT_ROOT, "diary", f"{today_str}.md")

    supabase_line = f"今日 Supabase 录入 {count} 次"
    if os.path.exists(diary_path):
        with open(diary_path, "r", encoding="utf-8") as f:
            content = f.read()
    else:
        content = f"# {today_str}\n\n## 日记\n"

    import re as _re
    if _re.search(r'今日 Supabase 录入 \d+ 次', content):
        content = _re.sub(r'今日 Supabase 录入 \d+ 次', supabase_line, content)
    else:
        content = content.rstrip() + f"\n\n{supabase_line}\n"

    try:
        from delegate_tools import atomic_write_text
        atomic_write_text(diary_path, content)
        print(f"[Supabase计数] 今日 {count} 次 → {today_str}.md")
    except Exception as e:
        print(f"[Supabase计数] 写入日记失败: {e}")


def main():
    # ── PID 文件锁：防止多个轮询守护同时运行 ──
    PID_FILE = os.path.join(PROJECT_ROOT, ".polling_loop.pid")
    if os.path.exists(PID_FILE):
        try:
            with open(PID_FILE, "r") as pf:
                old_pid = int(pf.read().strip())
            import ctypes
            SYNCHRONIZE = 0x00100000
            PROCESS_QUERY_LIMITED = 0x1000
            kernel32 = ctypes.windll.kernel32
            h = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED, False, old_pid)
            if h:
                kernel32.CloseHandle(h)
                print(f"[轮询守护] 已有实例在运行 (PID {old_pid})，退出。")
                return
        except Exception:
            pass
    with open(PID_FILE, "w") as pf:
        pf.write(str(os.getpid()))
    import atexit
    atexit.register(lambda: os.path.exists(PID_FILE) and os.remove(PID_FILE))

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

    # ── 启动查补：日记 + 矿工 ──
    _ensure_diary(chain_dream, reason="startup_catchup")
    _ensure_miner()

    # ── 启动自检：老卡 link 回填（link 表为空时自动重建） ──
    try:
        from memory.linker import ensure_link_table
        ensure_link_table()
        import sqlite3 as _l_sql
        _ldb = _l_sql.connect(os.path.join(PROJECT_ROOT, "memory", "cards.db"))
        _lcur = _ldb.cursor()
        _lcur.execute("SELECT COUNT(*) FROM card_links")
        _link_count = _lcur.fetchone()[0]
        _lcur.execute("SELECT COUNT(*) FROM cards WHERE review_status='final' AND embedding IS NOT NULL")
        _vec_count = _lcur.fetchone()[0]
        _ldb.close()
        if _link_count == 0 and _vec_count >= 2:
            print(f"[link回填] link 表为空 ({_vec_count} 张有向量卡片)，正在重建...")
            _log_event("link_rebuild_start", {"vec_count": _vec_count})
            from memory.linker import rebuild_all_links
            _built = rebuild_all_links()
            _log_event("link_rebuild_done", {"edges": _built})
            print(f"[link回填] 完成: {_built} 条边")
        else:
            print(f"[link回填] 已就绪: {_link_count} 条边 ({_vec_count} 张向量卡片)")
    except Exception as _le:
        print(f"[link回填] 跳过: {_le}")
        _log_event("link_rebuild_error", {"error": str(_le)[:200]})

    # ── 待办提醒：扫描 todo/commitments 卡，定时推送 ──
    REMINDED_PATH = os.path.join(PROJECT_ROOT, "memory", "reminded_todos.json")
    REMINDER_COOLDOWN = 60  # 同一张卡至少间隔 60 分钟再提醒

    def _check_todo_reminders(now_local):
        """扫描到期待办，Bark 推送提醒。返回提醒数量。"""
        import sqlite3, json as _json
        from datetime import datetime as _dt, timedelta as _td
        with open(os.path.join(PROJECT_ROOT, "config.json"), "r", encoding="utf-8") as _f:
            config = json.load(_f)
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
        rows = c.fetchall()
        # 每轮输出待办池概览
        if rows:
            from shared import trace as _tr_scan
            card_list = [f"{r['title'][:20]}({r['target_date'] or '无日期'})" for r in rows[:5]]
            _tr_scan("bark_pool", f"扫描{len(rows)}张待办: {', '.join(card_list)}")
        for row in rows:
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
                    diff = target_minutes - now_minutes
                    if 0 <= diff <= 30:
                        should_remind = True
                        remind_reason = f"{target_time.strftime('%H:%M')} 到期"
                        from shared import trace
                        trace("bark_scan", f"命中「{ctitle}」→ {target_time.strftime('%H:%M')} (diff={diff}min)")
                except Exception:
                    pass
            # 今天到期的纯日期（全天待办，早9点提醒）
            elif tdate == today_str:
                if 8 <= now_local.hour <= 10:
                    should_remind = True
                    remind_reason = f"今天待办"
                    from shared import trace
                    trace("bark_scan", f"全天待办「{ctitle}」→ 早间提醒")
            # 即将到期（明天）
            elif tdate:
                try:
                    target_dt = _dt.fromisoformat(tdate)
                    if (target_dt - now_local).days <= 1 and now_local.hour >= 20:
                        should_remind = True
                        remind_reason = f"明天 {target_dt.strftime('%H:%M')} 到期"
                        from shared import trace
                        trace("bark_scan", f"即将到期「{ctitle}」→ 晚间提醒")
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
                    from shared import record_bark_push
                    record_bark_push(msg)
                except Exception as e:
                    print(f"[待办提醒] 推送失败: {e}")
        conn.close()

        # ── pending 扫描：待审核卡也有 target_date，同样推到 Bark ──
        pending_path = os.path.join(PROJECT_ROOT, "memory", "pending_cards.json")
        if os.path.exists(pending_path):
            try:
                with open(pending_path, "r", encoding="utf-8") as _pf:
                    _pendings = _json.load(_pf)
                for _pc in _pendings:
                    _pcat = _pc.get("category", "")
                    if _pcat not in ('todo', 'commitments', 'daily_life'):
                        continue
                    _ptdate = _pc.get("target_date", "") or ""
                    if not _ptdate:
                        continue
                    _pcid = _pc.get("id", "")
                    # 冷却检查
                    if _pcid in reminded:
                        _plast = _dt.fromisoformat(reminded[_pcid])
                        if (now_local - _plast).total_seconds() < REMINDER_COOLDOWN * 60:
                            continue
                    from shared import trace as _tr_p
                    _tr_p("bark_pending", f"扫描 {_pc.get('title','?')[:30]} tdate={_ptdate}")
                    _pshould = False
                    _preason = ""
                    # 带时间的今天到期
                    if _ptdate.startswith(today_str) and " " in _ptdate:
                        try:
                            _ptime = _dt.fromisoformat(_ptdate)
                            _pdiff = (_ptime.hour * 60 + _ptime.minute) - now_minutes
                            if 0 <= _pdiff <= 30:
                                _pshould = True
                                _preason = f"{_ptime.strftime('%H:%M')} 到期"
                        except Exception:
                            pass
                    elif _ptdate == today_str:
                        if 8 <= now_local.hour <= 10:
                            _pshould = True
                            _preason = "今天待办"
                    elif _ptdate:
                        try:
                            _ptdt = _dt.fromisoformat(_ptdate)
                            if (_ptdt - now_local).days <= 1 and now_local.hour >= 20:
                                _pshould = True
                                _preason = f"明天 {_ptdt.strftime('%H:%M')} 到期"
                        except Exception:
                            pass
                    if not _pshould:
                        continue
                    if bark_key and bark_key != "你的BarkKey填这里":
                        import requests as _req_p
                        try:
                            _pmsg = f"⏰ [待审核] {_pc.get('title','?')} — {_preason}"
                            _req_p.get(f"https://api.day.app/{bark_key}/{_pmsg}", timeout=10)
                            pushed += 1
                            reminded[_pcid] = now_local.isoformat()
                            print(f"[待办提醒-pending] {_pc.get('title','?')} — {_preason}")
                            _log_event("todo_reminder", {"card_id": _pcid, "title": str(_pc.get('title',''))[:40], "source": "pending"})
                            from shared import record_bark_push
                            record_bark_push(_pmsg)
                        except Exception:
                            pass
            except Exception as _pe:
                print(f"[待办提醒-pending] 扫描跳过: {_pe}")

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
    last_audit_time = time.time()  # 深渊审计
    last_housekeeping = 0  # 日常维护（日记/矿工/周收拢）

    cycle_count = 0

    while True:
        cycle_count += 1
        now_ts = time.time()

        try:
            print(f"\n{'='*40}")
            bark_main()
        except KeyboardInterrupt:
            print("\n[DSphantom轮询守护] 已停止。")
            _log_event("polling_stop", {"reason": "user_interrupt"})
            break
        except Exception as e:
            print(f"[DSphantom轮询守护] 异常: {e}")
            traceback.print_exc()
            _log_event("polling_crash", {"error": str(e)[:200]})

        # ── 心跳日志（每30分钟） ──
        if now_ts - last_heartbeat > HEARTBEAT_INTERVAL:
            _log_event("polling_heartbeat", {"cycles_since_last": cycle_count})
            last_heartbeat = now_ts
            cycle_count = 0

        # ── 日常维护：日记/矿工/周收拢（每15分钟执行一次） ──
        HOUSEKEEPING_INTERVAL = 15 * 60
        if now_ts - last_housekeeping > HOUSEKEEPING_INTERVAL:
            last_housekeeping = now_ts

            # 日记：昨天缺失则生成
            _ensure_diary(chain_dream, reason="scheduled")

            # 矿工：今天未蒸馏则触发
            _ensure_miner()

            # Supabase 录入计数：同步当日日记
            _sync_supabase_count()

            # 周收拢：距上次收拢 >= 7 天则触发（读文件名日期，抗重启）
            try:
                import glob as _glob_sweep, re as _re_sweep
                diary_dir_sweep = os.path.join(PROJECT_ROOT, "diary")
                weekly_files = sorted(_glob_sweep.glob(
                    os.path.join(diary_dir_sweep, "weekly_*.md")), reverse=True)
                last_sweep_date = None
                if weekly_files:
                    m = _re_sweep.search(r'weekly_(\d{4}-\d{2}-\d{2})', weekly_files[0])
                    if m:
                        last_sweep_date = m.group(1)
                today_str = beijing_today()
                from datetime import datetime as _dt_sweep
                need_sweep = True
                if last_sweep_date:
                    need_sweep = (_dt_sweep.strptime(today_str, "%Y-%m-%d")
                                  - _dt_sweep.strptime(last_sweep_date, "%Y-%m-%d")).days >= 7
                if need_sweep:
                    days_since = "首次" if not last_sweep_date else str(
                        (_dt_sweep.strptime(today_str, "%Y-%m-%d")
                         - _dt_sweep.strptime(last_sweep_date, "%Y-%m-%d")).days)
                    print(f"[周收拢] 聚合近7天待办（距上次 {days_since} 天）...")
                    _log_event("sweep_scheduled", {"last_sweep_date": last_sweep_date or "无"})
                    weekly_sweep()
            except Exception as e:
                _log_event("sweep_error", {"error": str(e)[:200]})

        # ── 深渊审计（每6小时） ──
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

        # ── 待办提醒扫描 ──
        _check_todo_reminders(beijing_now())

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