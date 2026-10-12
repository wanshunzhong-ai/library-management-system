"""赔偿处理数据访问层。"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from library import db

_SELECT = """
SELECT c.id, c.loan_id, c.member_id, c.book_id, c.kind, c.amount, c.paid_amount,
       c.status, c.note, c.operator, c.settled_at, c.created_at, c.updated_at,
       b.title    AS book_title,
       b.barcode  AS book_barcode,
       b.isbn     AS book_isbn,
       m.name     AS member_name,
       m.card_no  AS member_card,
       l.borrowed_at AS loan_borrowed_at,
       l.due_at      AS loan_due_at,
       l.returned_at AS loan_returned_at
  FROM compensations c
  JOIN books   b ON b.id = c.book_id
  JOIN members m ON m.id = c.member_id
  JOIN loans   l ON l.id = c.loan_id
"""

_SORT_COLUMNS = {
    "id": "c.id",
    "amount": "c.amount",
    "created_at": "c.created_at",
    "status": (
        "CASE c.status WHEN 'pending' THEN 0 WHEN 'settled' THEN 1 ELSE 2 END"
    ),
}
_DEFAULT_ORDER = "c.id DESC"


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    status = (filters.get("status") or "").strip()
    if status:
        clauses.append("c.status = ?")
        params.append(status)

    kind = (filters.get("kind") or "").strip()
    if kind:
        clauses.append("c.kind = ?")
        params.append(kind)

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append(
            "(b.title LIKE ? OR b.barcode LIKE ? OR b.isbn LIKE ? "
            " OR m.name LIKE ? OR m.card_no LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like, like])

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def count(filters: Mapping[str, Any]) -> int:
    where, params = _build_filters(filters)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM compensations c{where}", params) or 0)


def list_rows(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters)
    order = db.order_clause(filters, _SORT_COLUMNS, _DEFAULT_ORDER)
    sql = f"{_SELECT}{where} ORDER BY {order} LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def summaries(filters: Mapping[str, Any]) -> dict[str, Any]:
    """按当前筛选汇总赔付金额（列表页头部展示用）。"""
    where, params = _build_filters(filters)
    row = db.query_one(
        "SELECT COUNT(*) AS total, "
        "       COALESCE(SUM(c.amount), 0) AS amount_total, "
        "       COALESCE(SUM(c.paid_amount), 0) AS paid_total, "
        "       COALESCE(SUM(CASE WHEN c.status = 'pending' THEN c.amount ELSE 0 END), 0) "
        "           AS pending_amount "
        f"  FROM compensations c{where}",
        params,
    ) or {}
    return {
        "total": int(row.get("total") or 0),
        "amount_total": round(float(row.get("amount_total") or 0), 2),
        "paid_total": round(float(row.get("paid_total") or 0), 2),
        "pending_amount": round(float(row.get("pending_amount") or 0), 2),
    }


def get(comp_id: int) -> dict[str, Any] | None:
    return db.query_one(f"{_SELECT} WHERE c.id = ?", [comp_id])


def get_for_update(conn, comp_id: int) -> dict[str, Any] | None:
    """事务内读（只用调用方的连接，不自己开事务）。"""
    cur = conn.execute(f"{_SELECT} WHERE c.id = ?", [comp_id])
    try:
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        cur.close()


def find_open_by_loan(loan_id: int) -> dict[str, Any] | None:
    return db.query_one(
        f"{_SELECT} WHERE c.loan_id = ? AND c.status = 'pending'", [loan_id]
    )


def insert(conn, payload: Mapping[str, Any]) -> int:
    cur = conn.execute(
        "INSERT INTO compensations "
        "(loan_id, member_id, book_id, kind, amount, paid_amount, status, note, operator) "
        "VALUES (:loan_id, :member_id, :book_id, :kind, :amount, 0, 'pending', :note, :operator)",
        dict(payload),
    )
    try:
        return int(cur.lastrowid or 0)
    finally:
        cur.close()


def mark_settled(
    conn, comp_id: int, *, paid_amount: float, note: str, operator: str
) -> int:
    cur = conn.execute(
        "UPDATE compensations SET status = 'settled', paid_amount = ?, "
        "       note = CASE WHEN ? = '' THEN note ELSE ? END, operator = ?, "
        "       settled_at = datetime('now', 'localtime'), "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'pending'",
        [paid_amount, note, note, operator, comp_id],
    )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def mark_cancelled(conn, comp_id: int, *, note: str, operator: str) -> int:
    cur = conn.execute(
        "UPDATE compensations SET status = 'cancelled', "
        "       note = CASE WHEN ? = '' THEN note ELSE ? END, operator = ?, "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'pending'",
        [note, note, operator, comp_id],
    )
    try:
        return max(0, int(cur.rowcount or 0))
    finally:
        cur.close()


def count_pending_for_member(member_id: int) -> int:
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM compensations WHERE member_id = ? AND status = 'pending'",
            [member_id],
        )
        or 0
    )


def list_pending_for_member(member_id: int) -> list[dict[str, Any]]:
    return db.query_all(
        f"{_SELECT} WHERE c.member_id = ? AND c.status = 'pending' ORDER BY c.id DESC",
        [member_id],
    )


def by_ids(ids: Sequence[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    return db.query_all(f"{_SELECT} WHERE c.id IN ({placeholders})", list(ids))
