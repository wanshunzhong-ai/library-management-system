"""采编验收数据访问层。"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_SELECT = """
SELECT a.id, a.isbn, a.title, a.author, a.publisher, a.category, a.publish_year,
       a.location, a.copies, a.unit_price, a.supplier, a.note, a.status,
       a.book_id, a.reject_reason, a.reviewed_by, a.reviewed_at,
       a.created_at, a.updated_at,
       b.barcode AS book_barcode,
       b.total_copies AS book_total_copies
  FROM acquisitions a
  LEFT JOIN books b ON b.id = a.book_id
"""

_SORT_COLUMNS = {
    "id": "a.id",
    "title": "a.title COLLATE NOCASE",
    "copies": "a.copies",
    "unit_price": "a.unit_price",
    "created_at": "a.created_at",
    "status": "CASE a.status WHEN 'pending' THEN 0 WHEN 'accepted' THEN 1 ELSE 2 END",
}
_DEFAULT_ORDER = "a.id DESC"


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    status = (filters.get("status") or "").strip()
    if status:
        clauses.append("a.status = ?")
        params.append(status)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append(
            "(a.title LIKE ? OR a.author LIKE ? OR a.isbn LIKE ? "
            " OR a.supplier LIKE ? OR a.publisher LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def count(filters: Mapping[str, Any]) -> int:
    where, params = _build_filters(filters)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM acquisitions a{where}", params) or 0)


def list_rows(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters)
    order = db.order_clause(filters, _SORT_COLUMNS, _DEFAULT_ORDER)
    return db.query_all(f"{_SELECT}{where} ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset])


def summaries(filters: Mapping[str, Any]) -> dict[str, Any]:
    where, params = _build_filters(filters)
    row = db.query_one(
        "SELECT COUNT(*) AS total, "
        "  COALESCE(SUM(CASE WHEN a.status = 'pending' THEN 1 ELSE 0 END), 0) AS pending, "
        "  COALESCE(SUM(CASE WHEN a.status = 'accepted' THEN a.copies ELSE 0 END), 0) AS accepted_copies, "
        "  COALESCE(SUM(CASE WHEN a.status = 'accepted' THEN a.copies * a.unit_price ELSE 0 END), 0) AS amount "
        f" FROM acquisitions a{where}",
        params,
    ) or {}
    return {
        "total": int(row.get("total") or 0),
        "pending": int(row.get("pending") or 0),
        "accepted_copies": int(row.get("accepted_copies") or 0),
        "amount": round(float(row.get("amount") or 0), 2),
    }


def get(acq_id: int) -> dict[str, Any] | None:
    return db.query_one(_SELECT + " WHERE a.id = ?", [acq_id])


def get_for_update(conn, acq_id: int) -> dict[str, Any] | None:
    cur = conn.execute(_SELECT + " WHERE a.id = ?", [acq_id])
    try:
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        cur.close()


def insert(conn, payload: Mapping[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO acquisitions "
        "(isbn, title, author, publisher, category, publish_year, location, copies, "
        " unit_price, supplier, note, status, reviewed_by) "
        "VALUES (:isbn, :title, :author, :publisher, :category, :publish_year, :location, "
        "        :copies, :unit_price, :supplier, :note, 'pending', :operator)",
        dict(payload),
    )
    try:
        return int(cur.lastrowid or 0)
    finally:
        cur.close()


def mark_accepted(conn, acq_id: int, *, book_id: int, reviewer: str) -> int:
    cur = conn.execute(
        "UPDATE acquisitions SET status = 'accepted', book_id = ?, reviewed_by = ?, "
        "       reviewed_at = datetime('now', 'localtime'), "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'pending'",
        [book_id, reviewer, acq_id],
    )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def mark_rejected(conn, acq_id: int, *, reason: str, reviewer: str) -> int:
    cur = conn.execute(
        "UPDATE acquisitions SET status = 'rejected', reject_reason = ?, reviewed_by = ?, "
        "       reviewed_at = datetime('now', 'localtime'), "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'pending'",
        [reason, reviewer, acq_id],
    )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def delete(acq_id: int) -> None:
    db.execute("DELETE FROM acquisitions WHERE id = ?", [acq_id])


def find_book_by_isbn(isbn: str) -> dict[str, Any] | None:
    if not isbn:
        return None
    return db.query_one("SELECT id, title, total_copies FROM books WHERE isbn = ?", [isbn])


def find_book_by_isbn_conn(conn, isbn: str) -> dict[str, Any] | None:
    if not isbn:
        return None
    cur = conn.execute("SELECT id, title, total_copies FROM books WHERE isbn = ?", [isbn])
    try:
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        cur.close()
