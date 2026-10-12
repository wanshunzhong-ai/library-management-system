"""盘点数据访问层。"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_BATCH_SELECT = """
SELECT b.id, b.code, b.location, b.status, b.note, b.operator, b.item_count,
       b.closed_at, b.created_at, b.updated_at,
       (SELECT COUNT(*) FROM stocktaking_items i
         WHERE i.batch_id = b.id AND i.counted_copies IS NOT NULL) AS counted_count,
       (SELECT COUNT(*) FROM stocktaking_items i
         WHERE i.batch_id = b.id AND i.counted_copies IS NOT NULL
           AND i.counted_copies <> i.expected_copies) AS diff_count
  FROM stocktaking_batches b
"""

_ITEM_SELECT = """
SELECT i.id, i.batch_id, i.book_id, i.expected_copies, i.counted_copies, i.note, i.updated_at,
       k.title   AS book_title,
       k.barcode AS book_barcode,
       k.isbn    AS book_isbn,
       k.author  AS book_author,
       k.category AS book_category,
       k.location AS book_location,
       k.total_copies AS book_total_copies,
       k.available_copies AS book_available_copies
  FROM stocktaking_items i
  JOIN books k ON k.id = i.book_id
"""


def count_batches(filters: Mapping[str, Any]) -> int:
    where, params = _batch_where(filters)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM stocktaking_batches b{where}", params) or 0)


def list_batches(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _batch_where(filters)
    return db.query_all(
        _BATCH_SELECT + where + " ORDER BY b.id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
    )


def get_batch(batch_id: int) -> dict[str, Any] | None:
    return db.query_one(_BATCH_SELECT + " WHERE b.id = ?", [batch_id])


def find_open_location(location: str) -> dict[str, Any] | None:
    return db.query_one(
        _BATCH_SELECT + " WHERE b.status = 'open' AND b.location = ?", [location]
    )


def count_batches_on(code_prefix: str) -> int:
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM stocktaking_batches WHERE code LIKE ?", [f"{code_prefix}%"]
        )
        or 0
    )


def insert_batch(conn, payload: Mapping[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO stocktaking_batches (code, location, status, note, operator, item_count) "
        "VALUES (:code, :location, 'open', :note, :operator, 0)",
        dict(payload),
    )
    try:
        return int(cur.lastrowid or 0)
    finally:
        cur.close()


def snapshot_items(conn, batch_id: int, location: str) -> int:
    """把范围内图书快照进明细（expected = 快照时刻的总册数），返回写入行数。"""
    if location:
        cur = conn.execute(
            "INSERT INTO stocktaking_items (batch_id, book_id, expected_copies) "
            "SELECT ?, id, total_copies FROM books WHERE location = ? AND total_copies > 0",
            [batch_id, location],
        )
    else:
        cur = conn.execute(
            "INSERT INTO stocktaking_items (batch_id, book_id, expected_copies) "
            "SELECT ?, id, total_copies FROM books WHERE total_copies > 0",
            [batch_id],
        )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def set_item_count(conn, batch_id: int, item_id: int, counted: int | None, note: str) -> int:
    cur = conn.execute(
        "UPDATE stocktaking_items SET counted_copies = ?, "
        "       note = CASE WHEN ? = '' THEN note ELSE ? END, "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND batch_id = ?",
        [counted, note, note, item_id, batch_id],
    )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def touch_batch(conn, batch_id: int) -> None:
    cur = conn.execute(
        "UPDATE stocktaking_batches SET updated_at = datetime('now', 'localtime') WHERE id = ?",
        [batch_id],
    )
    cur.close()


def close_batch(conn, batch_id: int) -> int:
    cur = conn.execute(
        "UPDATE stocktaking_batches SET status = 'closed', "
        "       closed_at = datetime('now', 'localtime'), "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'open'",
        [batch_id],
    )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def reopen_batch(conn, batch_id: int) -> int:
    cur = conn.execute(
        "UPDATE stocktaking_batches SET status = 'open', closed_at = NULL, "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'closed'",
        [batch_id],
    )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def delete_batch(batch_id: int) -> None:
    db.execute("DELETE FROM stocktaking_batches WHERE id = ?", [batch_id])


def list_items(batch_id: int, *, only_counted: bool = False) -> list[dict[str, Any]]:
    extra = " AND i.counted_copies IS NOT NULL" if only_counted else ""
    return db.query_all(
        _ITEM_SELECT + f" WHERE i.batch_id = ?{extra} ORDER BY i.id",
        [batch_id],
    )


def item(batch_id: int, item_id: int) -> dict[str, Any] | None:
    return db.query_one(_ITEM_SELECT + " WHERE i.id = ? AND i.batch_id = ?", [item_id, batch_id])


def locations() -> list[dict[str, Any]]:
    """可盘点的馆藏位置（含未标架位一项）。"""
    return db.query_all(
        "SELECT COALESCE(NULLIF(location, ''), '') AS name, COUNT(*) AS titles, "
        "       SUM(total_copies) AS copies "
        " FROM books WHERE total_copies > 0 GROUP BY name ORDER BY titles DESC, name ASC"
    )


def open_loans_for_book(conn, book_id: int) -> int:
    cur = conn.execute(
        "SELECT COUNT(*) FROM loans WHERE book_id = ? AND returned_at IS NULL", [book_id]
    )
    try:
        row = cur.fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        cur.close()


def _batch_where(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    status = (filters.get("status") or "").strip()
    if status in {"open", "closed"}:
        clauses.append("b.status = ?")
        params.append(status)
    location = (filters.get("location") or "").strip()
    if location:
        clauses.append("b.location = ?")
        params.append(location)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(b.code LIKE ? OR b.location LIKE ? OR b.note LIKE ? OR b.operator LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params
