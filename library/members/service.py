"""读者业务逻辑。"""

from __future__ import annotations

from typing import Any, Mapping

from config import get_config
from library import db
from library.errors import ConflictError, NotFoundError
from library.members import repository as repo
from library.validation import (
    optional_email,
    optional_int,
    optional_str,
    pagination,
    require_choice,
    require_str,
)

MEMBER_TYPES = {"学生", "教师", "社会", "VIP"}
MEMBER_STATUS = {"active", "frozen"}


def _normalize(payload: Mapping[str, Any], *, default_max_borrow: int) -> dict[str, Any]:
    return {
        "card_no": require_str(payload, "card_no", label="读者证号", max_length=32),
        "name": require_str(payload, "name", label="姓名", max_length=50),
        "phone": optional_str(payload, "phone", label="手机号", max_length=20),
        "email": optional_email(payload, "email"),
        "member_type": require_choice(payload, "member_type", label="读者类型", choices=MEMBER_TYPES),
        "status": require_choice(payload, "status", label="状态", choices=MEMBER_STATUS),
        "max_borrow": optional_int(
            payload, "max_borrow", label="可借上限", minimum=1, maximum=50, default=default_max_borrow
        ),
    }


def list_members(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {"keyword": query.get("keyword", ""), "status": query.get("status", "")}
    total = repo.count(filters)
    items = repo.list_members(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def get_member(member_id: int) -> dict[str, Any]:
    member = repo.get(member_id)
    if member is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    member["active_loans"] = repo.active_loan_count(member_id)
    member["overdue_loans"] = repo.overdue_count(member_id)
    return member


def create_member(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = _normalize(payload, default_max_borrow=get_config().default_max_borrow)
    if repo.exists_card(data["card_no"]):
        raise ConflictError(f"读者证号 {data['card_no']} 已存在", details={"field": "card_no"})
    member_id = repo.insert(data)
    return get_member(member_id)


def update_member(member_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    if repo.get(member_id) is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    data = _normalize(payload, default_max_borrow=get_config().default_max_borrow)
    if repo.exists_card(data["card_no"], exclude_id=member_id):
        raise ConflictError(f"读者证号 {data['card_no']} 已被占用", details={"field": "card_no"})

    active = repo.active_loan_count(member_id)
    if data["status"] == "frozen" and active:
        raise ConflictError(f"该读者还有 {active} 册未归还，请先还清再冻结")
    repo.update(member_id, data)
    return get_member(member_id)


def delete_member(member_id: int) -> None:
    if repo.get(member_id) is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    active = repo.active_loan_count(member_id)
    if active:
        raise ConflictError(f"该读者还有 {active} 册未归还，无法删除")
    if bool(db.query_scalar("SELECT 1 FROM loans WHERE member_id = ? LIMIT 1", [member_id])):
        raise ConflictError("该读者存在历史借阅记录，为保留档案不可删除；可将其状态置为冻结")
    repo.delete(member_id)
