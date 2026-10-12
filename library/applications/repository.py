"""办证申请数据访问层。

与 ``members`` 同库不同表：``member_applications`` 记"申请与审批"，``members`` 记
"已生效的读者档案"。两者只在审核通过的那一刻发生关联（``member_id``）。
"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_COLUMNS = (
    "id, code, name, phone, email, id_type, id_tail, member_type, note, status, "
    "reject_reason, member_id, card_no, reviewed_by, reviewed_at, created_at, updated_at"
)

_SORT_COLUMNS = {
    "id": "id",
    "name": "name COLLATE NOCASE",
    "status": "CASE status WHEN 'pending' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END",
    "created_at": "created_at",
}
_DEFAULT_ORDER = "CASE status WHEN 'pending' THEN 0 ELSE 1 END, id DESC"


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(name LIKE ? OR phone LIKE ? OR code LIKE ? OR card_no LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])

    status = (filters.get("status") or "").strip()
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def count(filters: Mapping[str, Any]) -> int:
    where, params = _build_filters(filters)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM member_applications{where}", params) or 0)


def counts_by_status() -> dict[str, int]:
    rows = db.query_all("SELECT status, COUNT(*) AS n FROM member_applications GROUP BY status")
    result = {"pending": 0, "approved": 0, "rejected": 0}
    for row in rows:
        result[str(row["status"])] = int(row["n"])
    return result


def list_all(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters)
    order = db.order_clause(filters, _SORT_COLUMNS, _DEFAULT_ORDER)
    sql = f"SELECT {_COLUMNS} FROM member_applications{where} ORDER BY {order} LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def get(application_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM member_applications WHERE id = ?", [application_id])


def get_by_code(code: str) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM member_applications WHERE code = ?", [code])


def pending_by_phone(phone: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT id, code, name, status, created_at FROM member_applications "
        " WHERE phone = ? AND status = 'pending' ORDER BY id DESC LIMIT 1",
        [phone],
    )


def insert(data: Mapping[str, Any]) -> int:
    return db.execute(
        """
        INSERT INTO member_applications
            (code, name, phone, email, id_type, id_tail, member_type, note, status)
        VALUES
            (:code, :name, :phone, :email, :id_type, :id_tail, :member_type, :note, 'pending')
        """,
        dict(data),
    )


def set_rejected(application_id: int, reason: str, reviewed_by: str) -> None:
    db.execute(
        "UPDATE member_applications SET status = 'rejected', reject_reason = ?, "
        "       reviewed_by = ?, reviewed_at = datetime('now', 'localtime'), "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'pending'",
        [reason, reviewed_by, application_id],
    )


# ---------------------------------------------------------------- 事务内使用
# 审核通过要「建读者 + 改申请单」两笔写，必须同一个事务。
# db.execute 自带事务，在外层 IMMEDIATE 里调用会把外层事务提交掉，
# 因此下面两个函数只接受调用方传入的 conn，自己不开事务。


def raw_for_update(conn, application_id: int) -> Mapping[str, Any] | None:
    cur = conn.execute(
        f"SELECT {_COLUMNS} FROM member_applications WHERE id = ?", [application_id]
    )
    try:
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        cur.close()


def mark_approved(conn, application_id: int, member_id: int, card_no: str, reviewed_by: str) -> int:
    cur = conn.execute(
        "UPDATE member_applications SET status = 'approved', member_id = ?, card_no = ?, "
        "       reviewed_by = ?, reviewed_at = datetime('now', 'localtime'), "
        "       updated_at = datetime('now', 'localtime') "
        " WHERE id = ? AND status = 'pending'",
        [member_id, card_no, reviewed_by, application_id],
    )
    affected = int(cur.rowcount or 0)
    cur.close()
    return affected


def taken_identifiers(conn) -> set[str]:
    """读者证号与管理员用户名（两者共用一个登录入口，不能重名）。"""
    used: set[str] = set()
    for table, column in (("members", "card_no"), ("admins", "username")):
        cur = conn.execute(f"SELECT {column} FROM {table}")
        try:
            used.update(str(row[0]).upper() for row in cur.fetchall())
        finally:
            cur.close()
    return used
