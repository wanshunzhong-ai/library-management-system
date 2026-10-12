"""读者数据访问层。"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

# 对外返回的字段（绝不包含 password_hash）
_PUBLIC_COLUMNS = (
    "id, card_no, name, phone, email, email_subscribed, member_type, status, max_borrow, "
    "card_lost, lost_at, must_change_password, last_login_at, created_at, updated_at"
)
# 鉴权专用：额外带出密码哈希，仅 reader 登录流程内部使用
_AUTH_COLUMNS = f"{_PUBLIC_COLUMNS}, password_hash"

# 允许前端排序的列（对外键 → 列/别名白名单）。能排序的别名必须出现在 list_members 的 SELECT 里。
_SORT_COLUMNS = {
    "id": "m.id",
    "card_no": "m.card_no COLLATE NOCASE",
    "name": "m.name COLLATE NOCASE",
    "member_type": "m.member_type COLLATE NOCASE",
    "status": "CASE m.status WHEN 'active' THEN 0 ELSE 1 END",
    "max_borrow": "m.max_borrow",
    "active_loans": "active_loans",
    "overdue_loans": "overdue_loans",
}
_DEFAULT_ORDER = "m.id DESC"


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
    order = db.order_clause(filters, _SORT_COLUMNS, _DEFAULT_ORDER)
    sql = (
        f"SELECT {', '.join('m.' + c.strip() for c in _PUBLIC_COLUMNS.split(','))}, "
        "       CASE WHEN m.password_hash = '' THEN 0 ELSE 1 END AS has_account, "
        "       (SELECT COUNT(*) FROM loans l WHERE l.member_id = m.id AND l.returned_at IS NULL) AS active_loans, "
        "       (SELECT COUNT(*) FROM loans l WHERE l.member_id = m.id AND l.returned_at IS NULL "
        "              AND date(l.due_at) < date('now', 'localtime')) AS overdue_loans "
        f"FROM members m{where} "
        f"ORDER BY {order} LIMIT ? OFFSET ?"
    )
    return db.query_all(sql, [*params, limit, offset])


def get(member_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_PUBLIC_COLUMNS} FROM members WHERE id = ?", [member_id])


def get_by_card(card_no: str) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_PUBLIC_COLUMNS} FROM members WHERE card_no = ?", [card_no])


def get_for_auth_by_account(account: str) -> dict[str, Any] | None:
    """按证号或手机号取读者（含密码哈希），供登录校验使用。

    证号与手机号都可以当登录账号，因此两者必须全局唯一（由 members/service
    在建档与改资料时校验 + 唯一索引兜底）。这里仍优先匹配证号，
    避免历史脏数据里两者交叉时出现不确定的身份归属。
    """
    sql = (
        f"SELECT {_AUTH_COLUMNS} FROM members "
        "WHERE card_no = :account OR (phone <> '' AND phone = :account) "
        "ORDER BY (card_no = :account) DESC, id LIMIT 1"
    )
    return db.query_one(sql, {"account": account})


def find_conflicting_identifier(value: str, *, exclude_id: int | None = None) -> dict[str, Any] | None:
    """该值是否已被**其他**读者的证号或手机号占用（登录标识必须全局唯一）。"""
    key = (value or "").strip()
    if not key:
        return None
    sql = "SELECT id, card_no, name, phone FROM members WHERE (card_no = ? OR phone = ?)"
    params: list[Any] = [key, key]
    if exclude_id is not None:
        sql += " AND id <> ?"
        params.append(exclude_id)
    return db.query_one(sql + " ORDER BY id LIMIT 1", params)


def exists_card(card_no: str, *, exclude_id: int | None = None) -> bool:
    if exclude_id is None:
        return bool(db.query_scalar("SELECT 1 FROM members WHERE card_no = ? LIMIT 1", [card_no]))
    return bool(db.query_scalar("SELECT 1 FROM members WHERE card_no = ? AND id <> ? LIMIT 1", [card_no, exclude_id]))


def insert(payload: Mapping[str, Any]) -> int:
    return db.execute(
        """
        INSERT INTO members
            (card_no, name, phone, email, member_type, status, max_borrow,
             password_hash, must_change_password)
        VALUES
            (:card_no, :name, :phone, :email, :member_type, :status, :max_borrow,
             :password_hash, :must_change_password)
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


def update_contact(member_id: int, phone: str, email: str, *, email_subscribed: bool | None = None) -> None:
    """读者自助修改联系方式与邮件订阅开关（姓名 / 证号 / 可借上限仍由管理员维护）。"""
    if email_subscribed is None:
        db.execute(
            "UPDATE members SET phone = ?, email = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            [phone, email, member_id],
        )
        return
    db.execute(
        "UPDATE members SET phone = ?, email = ?, email_subscribed = ?, "
        "updated_at = datetime('now', 'localtime') WHERE id = ?",
        [phone, email, 1 if email_subscribed else 0, member_id],
    )


def update_password_hash(member_id: int, password_hash: str) -> None:
    db.execute(
        "UPDATE members SET password_hash = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        [password_hash, member_id],
    )


def set_must_change_password(member_id: int, flag: bool) -> None:
    """置 / 清「首次登录必须改密」标记。

    置位发生在馆员建号、重置密码、办证发证三处——这三处的口令都是「证号后 6 位」
    这种可从证号直接推出来的弱口令。读者自己改密成功后清零。
    """
    db.execute(
        "UPDATE members SET must_change_password = ?, "
        "       updated_at = datetime('now', 'localtime') WHERE id = ?",
        [1 if flag else 0, member_id],
    )


def set_name(member_id: int, name: str) -> None:
    """馆员审核通过「姓名变更申请」后回写姓名。"""
    db.execute(
        "UPDATE members SET name = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        [name, member_id],
    )


def set_member_type(member_id: int, member_type: str) -> None:
    """馆员审核通过「读者类型变更申请」后回写类型。"""
    db.execute(
        "UPDATE members SET member_type = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
        [member_type, member_id],
    )


def set_card_lost(member_id: int, lost: bool) -> None:
    """借书证挂失 / 解挂。挂失时间只在挂失时写入，解挂清空。"""
    if lost:
        db.execute(
            "UPDATE members SET card_lost = 1, lost_at = datetime('now', 'localtime'), "
            "       updated_at = datetime('now', 'localtime') WHERE id = ?",
            [member_id],
        )
    else:
        db.execute(
            "UPDATE members SET card_lost = 0, lost_at = NULL, "
            "       updated_at = datetime('now', 'localtime') WHERE id = ?",
            [member_id],
        )


def touch_login(member_id: int) -> None:
    db.execute("UPDATE members SET last_login_at = datetime('now', 'localtime') WHERE id = ?", [member_id])


def readers_without_password() -> list[dict[str, Any]]:
    """尚未开通账号的读者（老库升级后需要回填初始密码）。"""
    return db.query_all(
        "SELECT id, card_no FROM members WHERE password_hash = '' ORDER BY id"
    )


def active_loan_count(member_id: int) -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM loans WHERE member_id = ? AND returned_at IS NULL", [member_id]) or 0)


def overdue_count(member_id: int) -> int:
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM loans "
            "WHERE member_id = ? AND returned_at IS NULL AND date(due_at) < date('now', 'localtime')",
            [member_id],
        )
        or 0
    )
