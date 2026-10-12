"""在线办证申请业务逻辑：访客自助提交 → 馆员审核发证。

关键取舍：

* **审核制而非直接发证**：读者证关系到借阅权限与赔偿责任，直接自助发证等于把
  实名核验环节整个跳过。访客提交的只是"申请"，证件由馆员现场核验后发放。
* **只存证件号后 4 位**：完整证件号不进系统，与 operation_logs 不抄业务字段的原则一致。
* **审核通过是"建读者 + 改申请单"两笔写**，必须同一个 IMMEDIATE 事务，
  否则并发下可能重复发证或留下孤儿档案。
* **提交与查询都按来源限流**：办证表单是唯一免登录的写入口，不限流就是一个
  任人灌数据的入口。
"""

from __future__ import annotations

import secrets
from typing import Any, Mapping

from config import get_config
from library import db, timeutil
from library.applications import repository as repo
from library.auth.passwords import hash_password, initial_password
from library.auth.throttle import LoginThrottle
from library.errors import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    ValidationError,
)
from library.members import service as member_service
from library.validation import (
    optional_email,
    optional_int,
    optional_str,
    pagination,
    require_choice,
    require_str,
)

STATUS_LABELS = {"pending": "待审核", "approved": "已通过", "rejected": "已驳回"}

ID_TYPES = ("身份证", "护照", "港澳台通行证", "其他")

# 自助申请的默认读者类型：必须是 members/service.MEMBER_TYPES 里的成员，
# 否则审核通过时会被建档案的校验挡下。「社会」是面向公众读者的通用类型。
DEFAULT_MEMBER_TYPE = "社会"

# 申请编号里的随机段去掉易混字符（0/O、1/I/L），方便读者照着念或抄写
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _apply_max_attempts(_key: str) -> int:
    return get_config().apply_max_attempts


def _apply_lock_seconds() -> int:
    return get_config().apply_lock_minutes * 60


# 提交与查询各一个桶：提交严、查询宽，互不连累
submit_throttle = LoginThrottle("member-application", max_attempts=_apply_max_attempts, lock_seconds=_apply_lock_seconds)
lookup_throttle = LoginThrottle("member-application-lookup", max_attempts=_apply_max_attempts, lock_seconds=_apply_lock_seconds)


def _guard(throttle: LoginThrottle, ip: str) -> None:
    key = LoginThrottle.ip_key(ip)
    remaining = throttle.remaining_lock(key)
    if remaining > 0:
        raise BusinessRuleError(f"操作过于频繁，请 {remaining} 秒后再试")
    throttle.record_failure(key)


def _normalize_phone(payload: Mapping[str, Any]) -> str:
    phone = require_str(payload, "phone", label="手机号", max_length=20)
    if not all(ch.isdigit() or ch in "+- " for ch in phone):
        raise ValidationError("手机号只能包含数字，可带 + - 分隔符", details={"field": "phone"})
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not 6 <= len(digits) <= 20:
        raise ValidationError("手机号位数不正确", details={"field": "phone"})
    return phone


def _decorate(item: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(item)
    row["status_label"] = STATUS_LABELS.get(str(row.get("status")), str(row.get("status") or ""))
    row["phone_masked"] = _mask_phone(str(row.get("phone") or ""))
    row["id_tail_display"] = f"****{row['id_tail']}" if row.get("id_tail") else "—"
    return row


def _mask_phone(phone: str) -> str:
    text = (phone or "").strip()
    if len(text) < 7:
        return text or "—"
    return f"{text[:3]}****{text[-4:]}"


def _public_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """访客查询自身申请进度时可见的字段（不含手机号、不含初始密码）。"""
    status = str(row.get("status"))
    view = {
        "code": row["code"],
        "name": row["name"],
        "member_type": row["member_type"],
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "created_at": row["created_at"],
        "reviewed_at": row.get("reviewed_at"),
    }
    if status == "approved":
        view["card_no"] = row.get("card_no") or ""
        view["hint"] = "申请已通过，请携带本人证件到馆领取读者证并获取初始密码。"
    elif status == "rejected":
        view["reject_reason"] = row.get("reject_reason") or ""
        view["hint"] = "很抱歉，本次申请未通过。可按驳回原因补充信息后重新提交。"
    else:
        view["hint"] = "申请已收到，馆员会尽快核验。请保存好申请编号以便查询进度。"
    return view


def get_application(application_id: int) -> dict[str, Any]:
    row = repo.get(application_id)
    if row is None:
        raise NotFoundError(f"办证申请 #{application_id} 不存在")
    return _decorate(row)


# ---------------------------------------------------------------- 访客侧


def submit(payload: Mapping[str, Any], *, ip: str = "") -> dict[str, Any]:
    """访客提交办证申请。返回申请编号（供后续查询进度）。"""
    _guard(submit_throttle, ip)

    name = require_str(payload, "name", label="姓名", max_length=50)
    phone = _normalize_phone(payload)
    email = optional_email(payload, "email")
    id_type = (
        require_choice(payload, "id_type", label="证件类型", choices=set(ID_TYPES))
        if payload.get("id_type")
        else ID_TYPES[0]
    )
    id_tail = optional_str(payload, "id_tail", label="证件号后 4 位", max_length=4)
    if id_tail and not id_tail.isalnum():
        raise ValidationError("证件号后 4 位只能是字母或数字", details={"field": "id_tail"})
    member_type = (
        require_choice(payload, "member_type", label="读者类型", choices=set(member_service.MEMBER_TYPES))
        if payload.get("member_type")
        else DEFAULT_MEMBER_TYPE
    )
    note = optional_str(payload, "note", label="备注", max_length=200)

    # 手机号既是联系方式也是登录账号：已办理过就不能重复申请
    existing = db.query_scalar("SELECT card_no FROM members WHERE phone = ? LIMIT 1", [phone])
    if existing:
        raise ConflictError(
            "该手机号已办理过读者证，可直接登录门户或到馆找回密码",
            details={"field": "phone"},
        )
    if repo.pending_by_phone(phone) is not None:
        raise ConflictError("该手机号已有待审核的申请，请耐心等待或凭申请编号查询进度", details={"field": "phone"})

    data = {
        "code": _generate_code(),
        "name": name,
        "phone": phone,
        "email": email,
        "id_type": id_type,
        "id_tail": id_tail,
        "member_type": member_type,
        "note": note,
    }
    repo.insert(data)
    return {"code": data["code"], "status": "pending", "status_label": STATUS_LABELS["pending"]}


def lookup(payload: Mapping[str, Any], *, ip: str = "") -> dict[str, Any]:
    """按「申请编号 + 手机号」查询进度。两个都要对上，避免单凭编号枚举。"""
    _guard(lookup_throttle, ip)

    code = require_str(payload, "code", label="申请编号", max_length=32).upper()
    phone = require_str(payload, "phone", label="手机号", max_length=20)

    row = repo.get_by_code(code)
    if row is None or str(row["phone"]) != phone:
        raise NotFoundError("未找到匹配的申请记录，请核对申请编号与手机号")
    return _public_view(row)


def _generate_code() -> str:
    stamp = timeutil.now().strftime("%Y%m%d")
    for _ in range(30):
        suffix = "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))
        code = f"AC{stamp}-{suffix}"
        if repo.get_by_code(code) is None:
            return code
    raise BusinessRuleError("申请编号生成失败，请稍后重试")


# ---------------------------------------------------------------- 馆员侧


def list_applications(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {
        "keyword": query.get("keyword", ""),
        "status": query.get("status", ""),
        "sort": query.get("sort", ""),
        "order": query.get("order", ""),
    }
    total = repo.count(filters)
    items = [_decorate(r) for r in repo.list_all(filters, limit=size, offset=(page - 1) * size)]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "status_counts": repo.counts_by_status(),
        "id_types": list(ID_TYPES),
        "member_types": sorted(member_service.MEMBER_TYPES),
    }


def _auto_card_no(conn) -> str:
    """自动证号：``R`` + 5 位序号，跳过读者证号与管理员用户名里已占用的。"""
    used = repo.taken_identifiers(conn)
    number = 1
    while f"R{number:05d}" in used:
        number += 1
    return f"R{number:05d}"


def approve(application_id: int, payload: Mapping[str, Any] | None = None, *, operator: str = "") -> dict[str, Any]:
    """审核通过：在同一事务里建读者档案并回填申请单。"""
    data = payload or {}
    cfg = get_config()

    with db.transaction(immediate=True) as conn:
        row = repo.raw_for_update(conn, application_id)
        if row is None:
            raise NotFoundError(f"办证申请 #{application_id} 不存在")
        if str(row["status"]) != "pending":
            label = STATUS_LABELS.get(str(row["status"]), str(row["status"]))
            raise ConflictError(f"该申请已是「{label}」，不能重复审核")

        card_no = optional_str(data, "card_no", label="读者证号", max_length=32) or _auto_card_no(conn)
        member_service.ensure_identifier_available(card_no, field="card_no", label="读者证号")
        member_service.ensure_identifier_available(
            str(row["phone"]), field="phone", label="手机号"
        )

        name = optional_str(data, "name", label="姓名", max_length=50) or str(row["name"])
        member_type = optional_str(data, "member_type", label="读者类型", max_length=20) or str(
            row["member_type"] or DEFAULT_MEMBER_TYPE
        )
        if member_type not in member_service.MEMBER_TYPES:
            raise ValidationError(
                f"读者类型只能是 {'/'.join(sorted(member_service.MEMBER_TYPES))} 之一",
                details={"field": "member_type"},
            )
        max_borrow = optional_int(
            data,
            "max_borrow",
            label="可借上限",
            minimum=1,
            maximum=50,
            default=cfg.default_max_borrow,
        )

        plain_password = initial_password(card_no)
        cur = conn.execute(
            "INSERT INTO members "
            "(card_no, name, phone, email, member_type, status, max_borrow, password_hash, "
            " must_change_password) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
            [
                card_no,
                name,
                str(row["phone"]),
                optional_email(data, "email") or str(row["email"] or ""),
                member_type,
                max_borrow,
                hash_password(plain_password) if plain_password else "",
                # 办证初始口令同样是「证号后 6 位」，要求读者首次登录即改
                1 if plain_password else 0,
            ],
        )
        member_id = int(cur.lastrowid or 0)
        cur.close()

        affected = repo.mark_approved(conn, application_id, member_id, card_no, operator)
        if not affected:  # 并发下已被别人审掉
            raise ConflictError("该申请刚被他人处理，请刷新后查看")

    member = member_service.get_member(member_id)
    member["initial_password"] = plain_password
    return {
        "application": get_application(application_id),
        "member": member,
        "card_no": card_no,
        "initial_password": plain_password,
    }


def reject(application_id: int, reason: str, *, operator: str = "") -> dict[str, Any]:
    row = repo.get(application_id)
    if row is None:
        raise NotFoundError(f"办证申请 #{application_id} 不存在")
    if str(row["status"]) != "pending":
        label = STATUS_LABELS.get(str(row["status"]), str(row["status"]))
        raise ConflictError(f"该申请已是「{label}」，不能重复审核")
    text = (reason or "").strip()
    if not text:
        raise ValidationError("请填写驳回原因，便于申请人补充材料", details={"field": "reason"})
    if len(text) > 200:
        raise ValidationError("驳回原因不能超过 200 个字符", details={"field": "reason"})
    repo.set_rejected(application_id, text, operator)
    return get_application(application_id)
