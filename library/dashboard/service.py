"""概览业务逻辑。"""

from __future__ import annotations

from typing import Any

from config import get_config
from library.books import service as book_service
from library.dashboard import repository as repo
from library.loans import repository as loan_repo
from library.loans import service as loan_service
from library.validation import optional_int


def overview(query: dict[str, Any] | None = None) -> dict[str, Any]:
    query = query or {}
    trend_days = optional_int(query, "trend_days", label="趋势天数", minimum=3, maximum=60, default=14) or 14
    cfg = get_config()

    return {
        "books": repo.book_stats(),
        "members": repo.member_stats(),
        "loans": repo.loan_stats(),
        "fine_estimate": round(repo.overdue_days_sum() * cfg.fine_per_day, 2),
        "policy": {
            "loan_days": cfg.loan_days,
            "max_renew": cfg.max_renew,
            "fine_per_day": cfg.fine_per_day,
            "default_max_borrow": cfg.default_max_borrow,
        },
        "trend": _fill_trend(loan_repo.borrow_activity(trend_days), trend_days),
        "category_ranking": loan_repo.category_ranking(),
        "hot_books": loan_repo.hot_books(),
        "recent_loans": loan_service.serialize_rows(loan_repo.recent(8)),
        "overdue_loans": loan_service.serialize_rows(loan_repo.overdue_list(10)),
        "due_soon_loans": loan_service.serialize_rows(loan_repo.due_soon(7, 8)),
        "categories": book_service.list_categories(),
    }


def _fill_trend(rows: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
    """把缺失的日期补成 0，保证前端折线不断档。"""
    from datetime import date, timedelta

    lookup = {row["day"]: int(row["count"]) for row in rows}
    today = date.today()
    series = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        key = day.strftime("%Y-%m-%d")
        series.append({"day": key, "label": day.strftime("%m-%d"), "count": lookup.get(key, 0)})
    return series
