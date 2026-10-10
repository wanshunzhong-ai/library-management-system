"""时间处理：数据库统一存 `YYYY-MM-DD HH:MM:SS`（本地时区）。"""

from __future__ import annotations

from datetime import date, datetime, timedelta

FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now()


def to_str(value: datetime) -> str:
    return value.strftime(FMT)


def parse(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip().replace("T", " ")
    for fmt in (FMT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def plus_days(value: datetime, days: int) -> datetime:
    return value + timedelta(days=days)


def date_diff(later: date, earlier: date) -> int:
    """返回天数差，恒为向后取整的比较（按自然日）。"""
    return (later - earlier).days
