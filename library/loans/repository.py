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
       m.phone      AS member_phone
  FROM loans l
  JOIN books   b ON b.id = l.book_id
  JOIN members m ON m.id = l.member_id
"""


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    status = (filters.get("status") or "").strip()
    if status == "borrowed":
        clauses.append("l.returned_at IS NULL AND l.due_at >= datetime('now', 'localtime')")
    elif status == "overdue":
        clauses.append("l.returned_at IS NULL AND l.due_at < datetime('now', 'localtime')")
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
    sql = (
        _SELECT
        + where
        + " ORDER BY (l.returned_at IS NULL) DESC, l.due_at ASC, l.id DESC LIMIT ? OFFSET ?"
    )
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
    return db.query_all(
        _SELECT
        + " WHERE l.returned_at IS NULL AND l.due_at >= datetime('now', 'localtime')"
          "   AND l.due_at < datetime('now', 'localtime', ?) "
          " ORDER BY l.due_at ASC LIMIT ?",
        [f"+{int(days)} days", limit],
    )


def overdue_list(limit: int = 20) -> list[dict[str, Any]]:
    return db.query_all(
        _SELECT
        + " WHERE l.returned_at IS NULL AND l.due_at < datetime('now', 'localtime')"
          " ORDER BY l.due_at ASC LIMIT ?",
        [limit],
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
