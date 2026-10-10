"""图书数据访问层：SQL 全部带参数绑定，过滤条件下推到数据库。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from library import db

_COLUMNS = (
    "id, isbn, title, author, publisher, category, publish_year, "
    "location, total_copies, available_copies, created_at, updated_at"
)


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(title LIKE ? OR author LIKE ? OR isbn LIKE ? OR publisher LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])

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


def list_books(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters)
    sql = (
        f"SELECT {_COLUMNS} FROM books{where} "
        "ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    return db.query_all(sql, [*params, limit, offset])


def get(book_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM books WHERE id = ?", [book_id])


def exists_isbn(isbn: str, *, exclude_id: int | None = None) -> bool:
    if exclude_id is None:
        return bool(db.query_scalar("SELECT 1 FROM books WHERE isbn = ? LIMIT 1", [isbn]))
    return bool(db.query_scalar("SELECT 1 FROM books WHERE isbn = ? AND id <> ? LIMIT 1", [isbn, exclude_id]))


def insert(payload: Mapping[str, Any]) -> int:
    return db.execute(
        """
        INSERT INTO books (isbn, title, author, publisher, category, publish_year,
                           location, total_copies, available_copies)
        VALUES (:isbn, :title, :author, :publisher, :category, :publish_year,
                :location, :total_copies, :available_copies)
        """,
        dict(payload),
    )


def update(book_id: int, payload: Mapping[str, Any]) -> None:
    data = dict(payload)
    data["id"] = book_id
    db.execute(
        """
        UPDATE books SET
            isbn = :isbn, title = :title, author = :author, publisher = :publisher,
            category = :category, publish_year = :publish_year, location = :location,
            total_copies = :total_copies, available_copies = :available_copies,
            updated_at = datetime('now', 'localtime')
        WHERE id = :id
        """,
        data,
    )


def delete(book_id: int) -> None:
    db.execute("DELETE FROM books WHERE id = ?", [book_id])


def active_loan_count(book_id: int) -> int:
    """该书当前未归还的借阅数量，用于判断能否删书/缩减库存。"""
    return int(db.query_scalar("SELECT COUNT(*) FROM loans WHERE book_id = ? AND returned_at IS NULL", [book_id]) or 0)


def adjust_available(book_id: int, delta: int) -> None:
    """库存增减，带下界保护。"""
    db.execute(
        """
        UPDATE books
           SET available_copies = available_copies + ?,
               updated_at = datetime('now', 'localtime')
         WHERE id = ? AND available_copies + ? >= 0
        """,
        [delta, book_id, delta],
    )


def categories() -> list[dict[str, Any]]:
    return db.query_all(
        "SELECT category AS name, COUNT(*) AS book_count, SUM(total_copies) AS copy_count "
        "FROM books GROUP BY category ORDER BY book_count DESC, name ASC"
    )


def list_by_ids(ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.query_all(f"SELECT {_COLUMNS} FROM books WHERE id IN ({placeholders})", list(ids))
    return {int(row["id"]): row for row in rows}
