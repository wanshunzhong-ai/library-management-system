"""读者数据访问层。"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_COLUMNS = (
    "id, card_no, name, phone, email, member_type, status, max_borrow, created_at, updated_at"
)


def _build_filters(filters: Mapping[str, Any], *, alias: str = "") -> tuple[str, list[Any]]:
    """生成 WHERE 子句，过滤条件下推到 SQL（不在应用层筛）。"""
    p = f"{alias}." if alias else ""
    clauses: list[str] = []
    params: list[Any] = []

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append(f"({p}name LIKE ? OR {p}card_no LIKE ? OR {p}phone LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])

    status = (filters.get("status") or "").strip()
    if status:
        clauses.append(f"{p}status = ?")
        params.append(status)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def count(filters: Mapping[str, Any]) -> int:
    where, params = _build_filters(filters)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM members{where}", params) or 0)


def list_members(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters, alias="m")
    sql = (
        "SELECT m.id, m.card_no, m.name, m.phone, m.email, m.member_type, m.status, m.max_borrow, "
        "       m.created_at, m.updated_at, "
        "       (SELECT COUNT(*) FROM loans l WHERE l.member_id = m.id AND l.returned_at IS NULL) AS active_loans, "
        "       (SELECT COUNT(*) FROM loans l WHERE l.member_id = m.id AND l.returned_at IS NULL "
        "              AND l.due_at < datetime('now', 'localtime')) AS overdue_loans "
        f"FROM members m{where} "
        "ORDER BY m.id DESC LIMIT ? OFFSET ?"
    )
    return db.query_all(sql, [*params, limit, offset])


def get(member_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM members WHERE id = ?", [member_id])


def get_by_card(card_no: str) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM members WHERE card_no = ?", [card_no])


def exists_card(card_no: str, *, exclude_id: int | None = None) -> bool:
    if exclude_id is None:
        return bool(db.query_scalar("SELECT 1 FROM members WHERE card_no = ? LIMIT 1", [card_no]))
    return bool(db.query_scalar("SELECT 1 FROM members WHERE card_no = ? AND id <> ? LIMIT 1", [card_no, exclude_id]))


def insert(payload: Mapping[str, Any]) -> int:
    return db.execute(
        """
        INSERT INTO members (card_no, name, phone, email, member_type, status, max_borrow)
        VALUES (:card_no, :name, :phone, :email, :member_type, :status, :max_borrow)
        """,
        dict(payload),
    )


def update(member_id: int, payload: Mapping[str, Any]) -> None:
    data = dict(payload)
    data["id"] = member_id
    db.execute(
        """
        UPDATE members SET
            card_no = :card_no, name = :name, phone = :phone, email = :email,
            member_type = :member_type, status = :status, max_borrow = :max_borrow,
            updated_at = datetime('now', 'localtime')
        WHERE id = :id
        """,
        data,
    )


def delete(member_id: int) -> None:
    db.execute("DELETE FROM members WHERE id = ?", [member_id])


def active_loan_count(member_id: int) -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM loans WHERE member_id = ? AND returned_at IS NULL", [member_id]) or 0)


def overdue_count(member_id: int) -> int:
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM loans "
            "WHERE member_id = ? AND returned_at IS NULL AND due_at < datetime('now', 'localtime')",
            [member_id],
        )
        or 0
    )
