"""读者门户的数据查询：只暴露公开字段，绝不返回读者等内部数据。"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_PUBLIC_COLUMNS = (
    "id, isbn, barcode, title, author, publisher, category, publish_year, location, "
    "description, total_copies, available_copies"
)


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append(
            "(title LIKE ? OR author LIKE ? OR publisher LIKE ? OR isbn LIKE ? "
            " OR barcode LIKE ? OR category LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like, like, like])

    category = (filters.get("category") or "").strip()
    if category:
        clauses.append("category = ?")
        params.append(category)

    if filters.get("available_only"):
        clauses.append("available_copies > 0")

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def count(filters: Mapping[str, Any]) -> int:
    where, params = _build_filters(filters)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM books{where}", params) or 0)


def search(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters)
    sql = (
        f"SELECT {_PUBLIC_COLUMNS} FROM books{where} "
        "ORDER BY (available_copies > 0) DESC, title ASC LIMIT ? OFFSET ?"
    )
    return db.query_all(sql, [*params, limit, offset])


def stats() -> dict[str, Any]:
    return db.query_one(
        """
        SELECT COUNT(*)                                   AS total_titles,
               COALESCE(SUM(total_copies), 0)             AS total_copies,
               COALESCE(SUM(available_copies), 0)         AS available_copies,
               COUNT(DISTINCT category)                   AS category_count,
               COALESCE(SUM(CASE WHEN available_copies > 0 THEN 1 ELSE 0 END), 0) AS borrowable_titles
          FROM books
        """
    ) or {}


def new_arrivals(limit: int = 6) -> list[dict[str, Any]]:
    return db.query_all(
        f"SELECT {_PUBLIC_COLUMNS} FROM books ORDER BY id DESC LIMIT ?", [limit]
    )


def recommended(limit: int = 6) -> list[dict[str, Any]]:
    """推荐口径：优先推荐「有可借库存 + 分类内借阅热度高」的书。"""
    return db.query_all(
        f"""
        SELECT b.id, b.isbn, b.barcode, b.title, b.author, b.publisher, b.category,
               b.publish_year, b.location, b.description, b.total_copies, b.available_copies,
               COALESCE(l.loan_count, 0) AS loan_count
          FROM books b
          LEFT JOIN (SELECT book_id, COUNT(*) AS loan_count FROM loans GROUP BY book_id) l
                 ON l.book_id = b.id
         WHERE b.available_copies > 0
         ORDER BY COALESCE(l.loan_count, 0) DESC, b.id DESC
         LIMIT ?
        """,
        [limit],
    )


def categories(limit: int = 12) -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT category AS name, COUNT(*) AS book_count, "
        "       COALESCE(SUM(available_copies), 0) AS available_copies "
        "FROM books GROUP BY category "
        "ORDER BY COALESCE(SUM(available_copies), 0) DESC, book_count DESC, name ASC LIMIT ?",
        [limit],
    )


def reader_count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM members") or 0)


def active_reader_count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM members WHERE status = 'active'") or 0)


def loan_total() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM loans") or 0)
