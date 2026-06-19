"""
clock.py — 全项目唯一的北京时间权威源
所有模块从此导入，不再各自计算时区。
"""
from datetime import datetime, timezone, timedelta

BJT = timezone(timedelta(hours=8))


def beijing_now() -> datetime:
    """返回当前北京时间（aware, tz=BJT）。"""
    return datetime.now(timezone.utc).astimezone(BJT)


def beijing_today() -> str:
    """当前北京日期 'YYYY-MM-DD'。"""
    return beijing_now().strftime("%Y-%m-%d")


def beijing_yesterday() -> str:
    """昨天北京日期 'YYYY-MM-DD'。"""
    return (beijing_now() - timedelta(days=1)).strftime("%Y-%m-%d")


def utc_ts_to_beijing_date(ts: str) -> str:
    """UTC时间戳字符串 → 北京时间日期 'YYYY-MM-DD'。
    支持 ISO 格式 (2026-06-12T19:38:40Z / +00:00 / 无时区=视为UTC)。
    解析失败返回空字符串。"""
    if not ts:
        return ""
    try:
        t = ts.strip()
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        bj = dt.astimezone(BJT)
        return bj.strftime("%Y-%m-%d")
    except Exception:
        return ""
