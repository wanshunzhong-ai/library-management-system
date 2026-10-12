"""罚金缴纳数据访问层。

只记「已经结算了多少」：``kind='payment'`` 是缴纳，``kind='waiver'`` 是减免，
两者都减少应缴；应缴金额本身不落库，由 ``loans`` + ``library.fine`` 实时计算。
"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_FROM = """
  FROM fine_payments p
  JOIN members m ON m.id = p.member_id
  JOIN loans   l ON l.id = p.loan_id
  JOIN books   b ON b.id = l.book_id
"""

_SELECT = """
SELECT p.id, p.member_id, p.loan_id, p.amount, p.kind, p.method, p.note,
       p.operator, p.created_at,
       m.card_no  AS member_card,
       m.name     AS member_name,
       b.title    AS book_title,
       b.isbn     AS book_isbn
""" + _FROM


def insert_payment(
    *,
    member_id: int,
    loan_id: int,
    amount: float,
    kind: str = "payment",
    method: str = "",
    note: str = "",
    operator: str = "",
) -> int:
    return db.execute(
        "INSERT INTO fine_payments (member_id, loan_id, amount, kind, method, note, operator) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [member_id, loan_id, round(float(amount), 2), kind, method, note, operator],
    )


def insert_payment_conn(
    conn,
    *,
    member_id: int,
    loan_id: int,
    amount: float,
    kind: str = "payment",
    method: str = "",
    note: str = "",
    operator: str = "",
) -> int:
    """用**调用方的连接**写入（跨模块联动用）。

    ``db.execute`` 自带事务，在外层 ``BEGIN IMMEDIATE`` 里调用会中途提交外层事务、
    悄悄破坏原子性；凡是「赔偿结案同时减免罚金」这类跨表写操作都必须走这个入口。
    """
    cur = conn.execute(
        "INSERT INTO fine_payments (member_id, loan_id, amount, kind, method, note, operator) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [member_id, loan_id, round(float(amount), 2), kind, method, note, operator],
    )
    try:
        return int(cur.lastrowid or 0)
    finally:
        cur.close()


def settled_by_loan(member_id: int) -> dict[int, float]:
    """该读者每笔借阅已结算（缴纳 + 减免）的金额。"""
    rows = db.query_all(
        "SELECT loan_id, SUM(amount) AS settled FROM fine_payments "
        " WHERE member_id = ? GROUP BY loan_id",
        [member_id],
    )
    return {int(row["loan_id"]): round(float(row["settled"] or 0), 2) for row in rows}


def settled_for_loan(loan_id: int) -> float:
    return round(
        float(
            db.query_scalar(
                "SELECT COALESCE(SUM(amount), 0) FROM fine_payments WHERE loan_id = ?", [loan_id]
            )
            or 0
        ),
        2,
    )


def summary_for_member(member_id: int) -> dict[str, float]:
    row = db.query_one(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN kind = 'payment' THEN amount ELSE 0 END), 0) AS paid, "
        "  COALESCE(SUM(CASE WHEN kind = 'waiver'  THEN amount ELSE 0 END), 0) AS waived "
        "FROM fine_payments WHERE member_id = ?",
        [member_id],
    ) or {}
    return {"paid": round(float(row.get("paid") or 0), 2), "waived": round(float(row.get("waived") or 0), 2)}


def history_for_member(member_id: int, *, limit: int = 20) -> list[dict[str, Any]]:
    return db.query_all(
        _SELECT + " WHERE p.member_id = ? ORDER BY p.id DESC LIMIT ?", [member_id, limit]
    )


def count_all(filters: Mapping[str, Any]) -> int:
    where, params = _where(filters)
    # 与 list_all 共用 _FROM：关键字过滤会命中 members / books 的列，
    # 少了 JOIN 时 keyword 非空就会报「no such column: m.name」。
    return int(db.query_scalar(f"SELECT COUNT(*) {_FROM}{where}", params) or 0)


def list_all(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _where(filters)
    return db.query_all(
        _SELECT + where + " ORDER BY p.id DESC LIMIT ? OFFSET ?", [*params, limit, offset]
    )


def totals(filters: Mapping[str, Any]) -> dict[str, float]:
    where, params = _where(filters)
    row = db.query_one(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN p.kind = 'payment' THEN p.amount ELSE 0 END), 0) AS paid, "
        "  COALESCE(SUM(CASE WHEN p.kind = 'waiver'  THEN p.amount ELSE 0 END), 0) AS waived "
        f"{_FROM}{where}",
        params,
    ) or {}
    return {"paid": round(float(row.get("paid") or 0), 2), "waived": round(float(row.get("waived") or 0), 2)}


def _where(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    kind = (filters.get("kind") or "").strip()
    if kind in {"payment", "waiver"}:
        clauses.append("p.kind = ?")
        params.append(kind)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(m.name LIKE ? OR m.card_no LIKE ? OR b.title LIKE ? OR p.operator LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params
