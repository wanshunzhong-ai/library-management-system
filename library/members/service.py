"""读者业务逻辑。"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from config import get_config
from library import db
from library.auth import repository as auth_repo
from library.auth.passwords import hash_password, initial_password
from library.errors import BusinessRuleError, ConflictError, NotFoundError
from library.members import repository as repo
from library.validation import (
    optional_email,
    optional_int,
    optional_str,
    pagination,
    require_choice,
    require_str,
)

logger = logging.getLogger(__name__)

MEMBER_TYPES = {"学生", "教师", "社会", "VIP"}
MEMBER_STATUS = {"active", "frozen"}


def ensure_identifier_available(
    value: str,
    *,
    field: str,
    label: str,
    exclude_member_id: int | None = None,
) -> None:
    """校验「证号 / 手机号」这个登录标识是否可用。

    证号与手机号都能当登录账号，因此必须**全局唯一**，且不能与后台管理员的
    用户名重名——否则统一登录入口识别身份时会互相遮蔽（管理员先匹配，
    读者将永远登不进来；反过来手机号撞上别人的证号则会登进他人账户）。
    管理员建档、管理员改档、读者自助改手机号三条路径共用本函数。
    """
    key = (value or "").strip()
    if not key:
        return
    if auth_repo.exists_username(key):
        raise ConflictError(
            f"{label} {key} 与后台管理员的登录账号重名，请更换",
            details={"field": field},
        )
    conflict = repo.find_conflicting_identifier(key, exclude_id=exclude_member_id)
    if conflict is not None:
        raise ConflictError(
            f"{label} {key} 已被读者「{conflict['name']}」占用（证号 {conflict['card_no']}），请更换",
            details={"field": field},
        )


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


def validate_member(payload: Mapping[str, Any]) -> dict[str, Any]:
    """只做规范化与唯一性校验，**不写库**（批量导入预览用）。

    与 ``create_member`` 共用 ``_normalize`` 与 ``ensure_identifier_available``，
    保证预览结论与真实导入结论一致。
    """
    data = _normalize(payload, default_max_borrow=get_config().default_max_borrow)
    if repo.exists_card(data["card_no"]):
        raise ConflictError(f"读者证号 {data['card_no']} 已存在", details={"field": "card_no"})
    ensure_identifier_available(data["card_no"], field="card_no", label="读者证号")
    ensure_identifier_available(data["phone"], field="phone", label="手机号")
    return data


def list_members(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {
        "keyword": query.get("keyword", ""),
        "status": query.get("status", ""),
        # 排序：未命中白名单时由仓库层回落到默认顺序
        "sort": query.get("sort", ""),
        "order": query.get("order", ""),
    }
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
    ensure_identifier_available(data["card_no"], field="card_no", label="读者证号")
    ensure_identifier_available(data["phone"], field="phone", label="手机号")

    # 新建读者即开通门户账号，初始密码为证号后 6 位；同时置「首次登录必须改密」——
    # 证号是顺序编号时，这串口令几乎等于公开信息，不能让它长期有效。
    plain_password = initial_password(data["card_no"])
    data["password_hash"] = hash_password(plain_password) if plain_password else ""
    data["must_change_password"] = 1 if plain_password else 0
    member_id = repo.insert(data)

    member = get_member(member_id)
    # 明文初始密码只在创建响应里返回一次，供管理员转告读者
    member["initial_password"] = plain_password
    return member


def reset_member_password(member_id: int) -> dict[str, Any]:
    """管理员把读者密码重置为初始密码（证号后 6 位）。"""
    member = repo.get(member_id)
    if member is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    plain = initial_password(str(member["card_no"]))
    if not plain:
        raise BusinessRuleError("该读者证号为空，无法生成初始密码")
    repo.update_password_hash(member_id, hash_password(plain))
    # 重置后的口令同样是「证号后 6 位」，必须强制读者下次登录就改掉
    repo.set_must_change_password(member_id, True)
    logger.info("重置读者密码", extra={"extra_fields": {"member_id": member_id, "card_no": member["card_no"]}})
    return {"member_id": member_id, "card_no": member["card_no"], "initial_password": plain}


def ensure_reader_accounts() -> int:
    """启动时为历史读者回填初始密码（老库升级后没有账号的读者）。"""
    pending = repo.readers_without_password()
    filled = 0
    for row in pending:
        plain = initial_password(str(row["card_no"]))
        if not plain:
            continue
        repo.update_password_hash(int(row["id"]), hash_password(plain))
        filled += 1
    if filled:
        logger.warning(
            "已为历史读者开通门户账号（初始密码 = 证号后 6 位），请提醒读者尽快修改",
            extra={"extra_fields": {"count": filled}},
        )
    return filled


def update_member(member_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    if repo.get(member_id) is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    data = _normalize(payload, default_max_borrow=get_config().default_max_borrow)
    if repo.exists_card(data["card_no"], exclude_id=member_id):
        raise ConflictError(f"读者证号 {data['card_no']} 已被占用", details={"field": "card_no"})
    ensure_identifier_available(
        data["card_no"], field="card_no", label="读者证号", exclude_member_id=member_id
    )
    ensure_identifier_available(
        data["phone"], field="phone", label="手机号", exclude_member_id=member_id
    )

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
