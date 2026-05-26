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
