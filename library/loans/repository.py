"""借阅数据访问层。"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_SELECT = """
SELECT l.id, l.book_id, l.member_id, l.borrowed_at, l.due_at, l.returned_at,
       l.renew_count, l.note,
       b.isbn       AS book_isbn,
       b.barcode    AS book_barcode,
       b.title      AS book_title,
       b.author     AS book_author,
       b.category   AS book_category,
       m.card_no    AS member_card,
       m.name       AS member_name,
       m.phone      AS member_phone,
       m.status     AS member_status,
       m.card_lost  AS member_card_lost
  FROM loans l
  JOIN books   b ON b.id = l.book_id
  JOIN members m ON m.id = l.member_id
"""

# 允许前端排序的列（对外键 → 列表达式白名单）。
# 状态与罚金是派生字段，这里用与之等价的 SQL 表达式排序：
#   状态序：逾期(0) → 在借(1) → 已归还(2)
#   罚金序：与「逾期天数」同序（应还与归还/今天的整日差，逾期为 0）
_OVERDUE_DAYS_SQL = (
    "CASE WHEN l.returned_at IS NULL "
    "     THEN MAX(0, CAST(julianday(date('now', 'localtime')) - julianday(date(l.due_at)) AS INTEGER)) "
    "     ELSE MAX(0, CAST(julianday(date(l.returned_at)) - julianday(date(l.due_at)) AS INTEGER)) END"
)
_SORT_COLUMNS = {
    "id": "l.id",
    "book_title": "b.title COLLATE NOCASE",
    "member_name": "m.name COLLATE NOCASE",
    "borrowed_at": "l.borrowed_at",
    "due_at": "l.due_at",
    "returned_at": "l.returned_at",
    "renew_count": "l.renew_count",
    "status": (
        "CASE WHEN l.returned_at IS NOT NULL THEN 2 "
        "     WHEN date(l.due_at) < date('now', 'localtime') THEN 0 ELSE 1 END"
    ),
    "fine": _OVERDUE_DAYS_SQL,
}
# 默认：未归还优先，其次按应还日期升序（最先到期的排前面），最后按 id 兜底稳定排序
_DEFAULT_ORDER = "(l.returned_at IS NULL) DESC, l.due_at ASC, l.id DESC"


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    # 逾期判定统一走「整日」口径（date 相减），与 service 层状态/天数算法一致：
    # 应还当天（无论时刻是否已过）都不算逾期，不会出现「逾期 0 天」。
    status = (filters.get("status") or "").strip()
    if status == "borrowed":
        clauses.append("l.returned_at IS NULL AND date(l.due_at) >= date('now', 'localtime')")
    elif status == "overdue":
        clauses.append("l.returned_at IS NULL AND date(l.due_at) < date('now', 'localtime')")
    elif status == "returned":
        clauses.append("l.returned_at IS NOT NULL")
    elif status == "open":
        clauses.append("l.returned_at IS NULL")

    if filters.get("member_id"):
        clauses.append("l.member_id = ?")
        params.append(int(filters["member_id"]))

    if filters.get("book_id"):
        clauses.append("l.book_id = ?")
        params.append(int(filters["book_id"]))

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append(
            "(b.title LIKE ? OR b.isbn LIKE ? OR b.barcode LIKE ? OR m.name LIKE ? OR m.card_no LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like, like])

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def count(filters: Mapping[str, Any]) -> int:
    where, params = _build_filters(filters)
    sql = "SELECT COUNT(*) FROM loans l JOIN books b ON b.id = l.book_id JOIN members m ON m.id = l.member_id" + where
    return int(db.query_scalar(sql, params) or 0)


def list_loans(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters)
    order = db.order_clause(filters, _SORT_COLUMNS, _DEFAULT_ORDER)
    sql = _SELECT + where + f" ORDER BY {order} LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def get(loan_id: int) -> dict[str, Any] | None:
    return db.query_one(_SELECT + " WHERE l.id = ?", [loan_id])


def raw(loan_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM loans WHERE id = ?", [loan_id])


def find_open_loan(book_id: int, member_id: int) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM loans WHERE book_id = ? AND member_id = ? AND returned_at IS NULL LIMIT 1",
        [book_id, member_id],
    )


def recent(limit: int = 8) -> list[dict[str, Any]]:
    return db.query_all(_SELECT + " ORDER BY l.id DESC LIMIT ?", [limit])


def due_soon(days: int = 7, limit: int = 8) -> list[dict[str, Any]]:
    """今天起 N 天内到期（整日口径，含「今天到期」，与逾期判定不重叠）。"""
    return db.query_all(
        _SELECT
        + " WHERE l.returned_at IS NULL AND date(l.due_at) >= date('now', 'localtime')"
          "   AND date(l.due_at) <= date('now', 'localtime', ?) "
          " ORDER BY l.due_at ASC LIMIT ?",
        [f"+{int(days)} days", limit],
    )


def overdue_list(limit: int = 20) -> list[dict[str, Any]]:
    return db.query_all(
        _SELECT
        + " WHERE l.returned_at IS NULL AND date(l.due_at) < date('now', 'localtime')"
          " ORDER BY l.due_at ASC LIMIT ?",
        [limit],
    )


def open_loan_settlements() -> list[dict[str, Any]]:
    """所有未归还记录（供概览按统一口径估算罚金）。"""
    return db.query_all(
        "SELECT due_at, returned_at FROM loans WHERE returned_at IS NULL"
    )


def borrow_activity(days: int = 14) -> list[dict[str, Any]]:
    """近 N 天每日借出量，用于概览趋势。"""
    return db.query_all(
        "SELECT date(borrowed_at) AS day, COUNT(*) AS count FROM loans "
        "WHERE borrowed_at >= datetime('now', 'localtime', ?) "
        "GROUP BY day ORDER BY day ASC",
        [f"-{int(days) - 1} days"],
    )


def category_ranking(limit: int = 6) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT b.category AS name, COUNT(*) AS loan_count "
        "FROM loans l JOIN books b ON b.id = l.book_id "
        "GROUP BY b.category ORDER BY loan_count DESC, name ASC LIMIT ?",
        [limit],
    )


def hot_books(limit: int = 6) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT b.id, b.title, b.author, COUNT(*) AS loan_count "
        "FROM loans l JOIN books b ON b.id = l.book_id "
        "GROUP BY b.id ORDER BY loan_count DESC, b.title ASC LIMIT ?",
        [limit],
    )
