"""后台账号管理 + 操作日志查询的业务逻辑。

与 ``library/auth``（"进来的这个人是谁"）分开：本模块关心"后台有几个人、
各是什么角色、谁动过什么"。

**最后管理员保护**是本模块的核心不变式：任何会让系统失去"最后一个在职系统
管理员"的操作都要被拒绝——否则一次误操作就能把所有人锁在后台之外，而且
没有任何界面能救回来（只能去改数据库）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Mapping

from library import audit, db
from library.auth import repository as repo
from library.auth.passwords import hash_password
from library.auth.permissions import (
    ROLE_ADMIN,
    ROLE_LABELS,
    capabilities_payload,
    role_label,
)
from library.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from library.validation import optional_str, pagination, require_str

logger = logging.getLogger(__name__)

MIN_PASSWORD_LENGTH = 6
MAX_DISPLAY_NAME = 40
MAX_USERNAME = 32
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{3,32}$")

LOG_ACTOR_TYPES = {"admin", "reader", "system"}
ACTOR_TYPE_LABELS = {"admin": "后台账号", "reader": "读者", "system": "系统"}


# ---------------------------------------------------------------- 输出整形


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["role_label"] = role_label(str(item.get("role") or ""))
    item["is_active"] = bool(item.get("is_active"))
    item["permissions"] = capabilities_payload(str(item.get("role") or ""))
    item.pop("password_hash", None)
    return item


def role_options() -> list[dict[str, Any]]:
    return [
        {"value": role, "label": label, "permissions": capabilities_payload(role)}
        for role, label in ROLE_LABELS.items()
    ]


def list_accounts() -> dict[str, Any]:
    items = [_public(row) for row in repo.list_public()]
    return {"items": items, "total": len(items), "roles": role_options()}


def get_account(account_id: int) -> dict[str, Any]:
    row = repo.get(account_id)
    if row is None:
        raise NotFoundError("账号不存在，可能已被删除")
    return _public(row)


# ---------------------------------------------------------------- 校验


def _validate_username(raw: Any, *, exclude_id: int | None = None) -> str:
    username = str(raw or "").strip()
    if not username:
        raise ValidationError("用户名不能为空", details={"field": "username"})
    if not _USERNAME_RE.match(username):
        raise ValidationError(
            f"用户名需为 3–{MAX_USERNAME} 位的字母、数字或 _ . @ - 组合",
            details={"field": "username"},
        )
    if repo.exists_username(username) and not _is_same_username(username, exclude_id):
        raise ConflictError(f"用户名「{username}」已被占用", details={"field": "username"})
    if repo.member_uses_identifier(username):
        raise ConflictError(
            f"「{username}」已被某位读者用作借书证号或手机号，两者共用同一登录入口，不能重名",
            details={"field": "username"},
        )
    return username


def _is_same_username(username: str, account_id: int | None) -> bool:
    if account_id is None:
        return False
    row = repo.get(int(account_id))
    return bool(row and str(row.get("username")) == username)


def _validate_password(raw: Any) -> str:
    password = str(raw or "")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"密码至少 {MIN_PASSWORD_LENGTH} 位", details={"field": "password"}
        )
    return password


def _validate_role(raw: Any) -> str:
    role = str(raw or "").strip()
    if role not in ROLE_LABELS:
        raise ValidationError(
            f"角色只能是 {'/'.join(ROLE_LABELS)} 之一", details={"field": "role"}
        )
    return role


def _guard_last_active_admin(target_id: int, *, remains_active_admin: bool, verb: str) -> None:
    """拒绝「把最后一个在职系统管理员」弄没的操作。"""
    if remains_active_admin:
        return
    if repo.count_active_admins(exclude_id=target_id) == 0:
        raise BusinessRuleError(
            f"这是最后一个在职的系统管理员，{verb}之后将没有人能管理后台账号；"
            "请先另建一个系统管理员"
        )


# ---------------------------------------------------------------- 写操作


def create_account(payload: Mapping[str, Any]) -> dict[str, Any]:
    username = _validate_username(payload.get("username"))
    password = _validate_password(payload.get("password"))
    role = _validate_role(payload.get("role") or ROLE_ADMIN)
    display_name = optional_str(
        payload, "display_name", label="姓名", max_length=MAX_DISPLAY_NAME
    ) or username
    account_id = repo.create(username, hash_password(password), display_name, role)
    logger.info(
        "已创建后台账号",
        extra={"extra_fields": {"username": username, "role": role, "account_id": account_id}},
    )
    return get_account(account_id)


def update_account(
    account_id: int, payload: Mapping[str, Any], *, actor_id: int | None = None
) -> tuple[dict[str, Any], list[str]]:
    """更新资料 / 角色 / 启停用。返回 ``(账号, 变更说明列表)``，后者用于审计摘要。"""
    row = repo.get(account_id)
    if row is None:
        raise NotFoundError("账号不存在，可能已被删除")

    display_name = optional_str(
        payload, "display_name", label="姓名", max_length=MAX_DISPLAY_NAME
    ) or str(row.get("display_name") or row.get("username") or "")
    role = (
        _validate_role(payload.get("role"))
        if payload.get("role") is not None
        else str(row.get("role") or ROLE_ADMIN)
    )
    is_active = (
        bool(payload.get("is_active"))
        if payload.get("is_active") is not None
        else bool(row.get("is_active"))
    )

    changes: list[str] = []
    if actor_id is not None and int(actor_id) == int(account_id):
        # 自己改自己的角色/启停用，等于给自己（或给下一个接管的人）挖坑，一律禁止；
        # 改自己的显示名没问题。
        if role != str(row.get("role")):
            raise BusinessRuleError("不能修改自己账号的角色，请让另一位系统管理员操作")
        if is_active != bool(row.get("is_active")):
            raise BusinessRuleError("不能停用自己的账号")

    _guard_last_active_admin(
        int(account_id),
        remains_active_admin=(role == ROLE_ADMIN and is_active),
        verb="停用或降级",
    )

    if display_name != str(row.get("display_name") or ""):
        changes.append(f"姓名：{row.get('display_name') or '—'} → {display_name}")
    if role != str(row.get("role")):
        changes.append(f"角色：{role_label(str(row.get('role')))} → {role_label(role)}")
    if is_active != bool(row.get("is_active")):
        changes.append("已启用" if is_active else "已停用")

    if not changes:
        return _public(row), changes

    repo.update_profile(account_id, display_name=display_name, role=role, is_active=is_active)
    logger.info(
        "已更新后台账号",
        extra={"extra_fields": {"account_id": account_id, "changes": changes}},
    )
    return get_account(account_id), changes


def set_password(account_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    if repo.get(account_id) is None:
        raise NotFoundError("账号不存在，可能已被删除")
    password = _validate_password(payload.get("password") or payload.get("new_password"))
    repo.update_password(account_id, hash_password(password))
    logger.info("已重置后台账号密码", extra={"extra_fields": {"account_id": account_id}})
    return get_account(account_id)


def delete_account(account_id: int, *, actor_id: int | None = None) -> dict[str, Any]:
    row = repo.get(account_id)
    if row is None:
        raise NotFoundError("账号不存在，可能已被删除")
    if actor_id is not None and int(actor_id) == int(account_id):
        raise BusinessRuleError("不能删除自己的账号")
    _guard_last_active_admin(int(account_id), remains_active_admin=False, verb="删除")
    repo.delete(account_id)
    logger.warning(
        "已删除后台账号", extra={"extra_fields": {"account_id": account_id, "username": row.get("username")}}
    )
    return _public(row)


# ---------------------------------------------------------------- 操作日志


def _log_filters(query: Mapping[str, Any]) -> tuple[str, list[Any]]:
    where: list[str] = []
    params: list[Any] = []

    keyword = optional_str(query, "keyword", label="关键词", max_length=60).strip()
    if keyword:
        like = f"%{keyword}%"
        where.append(
            "(summary LIKE ? OR action LIKE ? OR actor_name LIKE ? OR entity_id LIKE ?)"
        )
        params.extend([like, like, like, like])

    actor_type = optional_str(query, "actor_type", label="操作者类型", max_length=20).strip()
    if actor_type in LOG_ACTOR_TYPES:
        where.append("actor_type = ?")
        params.append(actor_type)

    status = optional_str(query, "status", label="结果", max_length=20).strip()
    if status in {"ok", "denied"}:
        where.append("status = ?")
        params.append(status)

    action = optional_str(query, "action", label="动作", max_length=60).strip()
    if action:
        where.append("action = ?")
        params.append(action)

    start = optional_str(query, "start", label="开始时间", max_length=20).strip()
    if start:
        where.append("created_at >= ?")
        params.append(start)
    end = optional_str(query, "end", label="结束时间", max_length=20).strip()
    if end:
        where.append("created_at <= ?")
        params.append(end)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return clause, params


def list_logs(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query, default_size=20, max_size=100)
    clause, params = _log_filters(query)
    total = int(db.query_scalar(f"SELECT COUNT(*) FROM operation_logs{clause}", params) or 0)
    rows = db.query_all(
        f"SELECT id, actor_type, actor_id, actor_name, actor_role, action, entity,"
        f"       entity_id, summary, detail, status, ip, created_at"
        f"  FROM operation_logs{clause} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, size, (page - 1) * size],
    )
    items = []
    for row in rows:
        item = dict(row)
        item["action_label"] = audit.action_label(str(item.get("action") or ""))
        item["entity_label"] = audit.entity_label(str(item.get("entity") or ""))
        item["actor_type_label"] = ACTOR_TYPE_LABELS.get(
            str(item.get("actor_type") or ""), str(item.get("actor_type") or "")
        )
        item["actor_display"] = item.get("actor_name") or item["actor_type_label"]
        items.append(item)

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        # renderPager 需要 pages 才能显示「第 x/y 页」
        "pages": max(1, (total + size - 1) // size),
        "actions": [
            {"value": action, "label": label}
            for action, label in sorted(
                audit.ACTION_LABELS.items(), key=lambda pair: pair[1]
            )
        ],
    }


def log_overview() -> dict[str, Any]:
    """日志维度的小统计，给运维一眼看个大概。"""
    return {
        "total": int(db.query_scalar("SELECT COUNT(*) FROM operation_logs") or 0),
        "denied": int(
            db.query_scalar("SELECT COUNT(*) FROM operation_logs WHERE status = 'denied'") or 0
        ),
        "today": int(
            db.query_scalar(
                "SELECT COUNT(*) FROM operation_logs WHERE date(created_at) = date('now', 'localtime')"
            )
            or 0
        ),
    }
