"""角色与权限：把「谁能改什么」变成代码里的约束，而不是口头约定。

背景：`admins.role` 从一开始就允许 `'admin' | 'librarian'`，但建表之后全仓库
没有任何一处读过它——后台是"单角色全权"，谁登录进来都能删书目、停用账号。
本模块把角色真正接到路由上。

设计取舍：

* **按能力（capability）授权，而不是按角色名硬编码**。路由声明自己需要
  ``book:write``，角色到能力的映射集中在一张表里；将来加"采编""馆长"只改映射，
  不动路由。
* **失败即拒绝**：角色不在表里（数据被手改、写了错别字）得到空能力集，
  什么都写不了，而不是默认放行。
* **越权尝试要留痕**：拦下来还不够，"谁试图做了什么"本身就是审计线索。
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Callable

from flask import jsonify

from library import audit

logger = logging.getLogger(__name__)

ROLE_ADMIN = "admin"
ROLE_LIBRARIAN = "librarian"

ROLE_LABELS: dict[str, str] = {
    ROLE_ADMIN: "系统管理员",
    ROLE_LIBRARIAN: "流通馆员",
}

# 能力 → 中文名（授权界面与错误提示都用它）
CAPABILITY_LABELS: dict[str, str] = {
    "book:read": "查看图书与馆藏",
    "book:write": "图书编目与上下架",
    "member:read": "查看读者档案",
    "member:write": "读者档案维护",
    "loan:read": "查看借阅 / 预约 / 罚金",
    "loan:write": "借书 / 还书 / 续借",
    "announcement:read": "查看公告",
    "announcement:write": "公告与开馆时间维护",
    "dashboard:read": "查看概览报表",
    "approval:approve": "审批罚金减免",
    "account:manage": "后台账号与授权管理",
    "log:read": "查看操作日志",
}

ALL_CAPABILITIES: tuple[str, ...] = (
    "book:read",
    "book:write",
    "member:read",
    "member:write",
    "loan:read",
    "loan:write",
    "announcement:read",
    "announcement:write",
    "dashboard:read",
    "approval:approve",
    "account:manage",
    "log:read",
)

# 角色 → 能力集。**改动这里就等于改动权限边界**，请连同 README 的权限矩阵一起改。
#
# 读 / 写分开声明的意义：日常角色（管理员、流通馆员）读写全给；
# 将来若加「只读审计 / 财务」岗位，只授 ``*:read`` 不授写能力即可——
# 各模块的列表 / 详情 GET 接口都已挂 ``*:read``，写接口挂 ``*:write``，互不牵连。
ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    # 系统管理员：全权，含账号与日志
    ROLE_ADMIN: frozenset(ALL_CAPABILITIES),
    # 流通馆员：日常业务全包（编目、办证、借还续、公告发布），
    # 但**不碰账号、不看全量日志**
    ROLE_LIBRARIAN: frozenset(
        {
            "book:read",
            "book:write",
            "member:read",
            "member:write",
            "loan:read",
            "loan:write",
            "announcement:read",
            "announcement:write",
            "dashboard:read",
        }
    ),
}


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role or "未知角色")


def capabilities_for(role: str) -> frozenset[str]:
    """角色具备的能力集。未知角色返回空集（失败即拒绝）。"""
    return ROLE_CAPABILITIES.get(str(role or ""), frozenset())


def has_permission(role: str, capability: str) -> bool:
    return capability in capabilities_for(role)


def capabilities_payload(role: str) -> list[str]:
    """/api/auth/me 返回给前端的权限列表（前端据此隐藏入口）。"""
    order = {name: index for index, name in enumerate(ALL_CAPABILITIES)}
    return sorted(capabilities_for(role), key=lambda name: order.get(name, 99))


def _forbidden(capabilities: tuple[str, ...], role: str):
    names = "、".join(CAPABILITY_LABELS.get(cap, cap) for cap in capabilities)
    return {
        "error": {
            "code": "forbidden",
            "message": f"当前角色（{role_label(role)}）没有「{names}」权限，请联系系统管理员",
        }
    }


def require_permission(*capabilities: str) -> Callable[[Callable], Callable]:
    """视图装饰器：要求当前管理员具备其中**任意一项**能力，否则 403 并留痕。

    必须挂在 ``@bp.post(...)`` 之内、业务函数之外（Flask 要求路由装饰器在最外层）。
    """

    needed = tuple(capabilities)

    def deco(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            from library.auth.service import current_admin  # 延迟导入避免循环依赖

            admin = current_admin()
            if admin is None:
                return (
                    jsonify({"error": {"code": "unauthorized", "message": "请先登录后再操作"}}),
                    401,
                )
            role = str(admin.get("role") or "")
            if not any(has_permission(role, cap) for cap in needed):
                audit.log(
                    "permission.denied",
                    entity="account",
                    entity_id=admin.get("id"),
                    summary=f"以「{role_label(role)}」身份尝试执行需「{'/'.join(needed)}」的操作，已拦截",
                    detail={"required": list(needed), "role": role, "endpoint": getattr(view, "__name__", "")},
                    status="denied",
                )
                logger.warning(
                    "权限不足已拦截",
                    extra={"extra_fields": {"username": admin.get("username"), "role": role, "required": list(needed)}},
                )
                return jsonify(_forbidden(needed, role)), 403

            return view(*args, **kwargs)

        return wrapper

    return deco
