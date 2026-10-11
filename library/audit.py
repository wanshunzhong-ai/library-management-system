"""操作审计：把「谁、在什么时候、对哪条记录、做了什么」落库。

分权的前提是能追责。只把角色拆开、不留操作痕迹，等于把权限从"口头约定"变成
"没人知道谁改的"——比不拆更糟。所以本模块与权限装饰器是配套的。

三条硬约束：

1. **审计失败不能拖垮业务**：写日志的任何异常都在这里吞掉并记 warning，
   绝不向调用方抛出——不能因为日志表写不进去就让一次正常的借书失败。
2. **不留业务快照**：只记「动作 + 目标记录 id + 一句话摘要」。把读者手机号、
   图书简介之类的全字段 JSON 复制进日志表，等于凭空多出一份不该有的个人信息。
3. **写日志必须在业务事务之外**：`db.execute` 自带事务，若在 `db.transaction()`
   内部调用，一旦外层回滚日志也会一起消失（或被算进同一把写锁）。装饰器挂在
   视图返回之后，天然在事务之外。
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Callable, Mapping

from library import db

logger = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 500
MAX_SUMMARY_CHARS = 200

# 动作 → 中文名。日志列表直接展示这个，避免运维看到 book.update 还要查文档。
ACTION_LABELS: dict[str, str] = {
    "book.create": "新增图书",
    "book.update": "修改图书",
    "book.shelf": "调整馆藏 / 上下架",
    "book.delete": "删除图书",
    "member.create": "新增读者",
    "member.update": "修改读者",
    "member.delete": "删除读者",
    "member.reset_password": "重置读者门户密码",
    "loan.borrow": "办理借书",
    "loan.return": "办理还书",
    "loan.return_batch": "批量还书",
    "loan.renew": "续借",
    "reservation.create": "读者提交预约",
    "reservation.cancel": "取消预约",
    "reservation.cancel_self": "读者取消预约",
    "reservation.ready": "预约到书通知",
    "reservation.fulfilled": "预约转为借出",
    "fine.pay": "读者自助缴纳滞纳金",
    "fine.collect": "馆员收取滞纳金",
    "fine.waive": "馆员减免滞纳金",
    "card.lost": "借书证挂失",
    "card.unlost": "解除借书证挂失",
    "reader.profile": "读者修改联系方式",
    "reader.password": "读者修改密码",
    "account.create": "新建后台账号",
    "account.update": "修改后台账号",
    "account.password": "重置后台账号密码",
    "account.delete": "删除后台账号",
    "auth.login": "登录后台",
    "auth.logout": "退出登录",
    "auth.password": "修改自己的登录密码",
    "permission.denied": "权限不足（已拦截）",
}

# 实体 → 中文名
ENTITY_LABELS: dict[str, str] = {
    "book": "图书",
    "member": "读者",
    "loan": "借阅记录",
    "reservation": "预约",
    "fine": "滞纳金",
    "account": "后台账号",
}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def entity_label(entity: str) -> str:
    return ENTITY_LABELS.get(entity, entity)


def _client_ip() -> str:
    try:
        from library.auth.service import client_ip

        return client_ip()
    except Exception:  # noqa: BLE001 - 审计字段，取不到就留空
        return ""


def current_actor() -> dict[str, Any]:
    """当前操作者。后台会话优先，其次读者会话，都没有则记为 system（脚本/CLI）。"""
    try:
        from library.auth.service import current_admin

        admin = current_admin()
    except Exception:  # noqa: BLE001 - 无请求上下文等
        admin = None
    if admin:
        return {
            "actor_type": "admin",
            "actor_id": admin.get("id"),
            "actor_name": admin.get("display_name") or admin.get("username") or "",
            "actor_role": admin.get("role") or "",
        }

    try:
        from library.reader.service import current_reader

        reader = current_reader()
    except Exception:  # noqa: BLE001
        reader = None
    if reader:
        return {
            "actor_type": "reader",
            "actor_id": reader.get("id"),
            "actor_name": reader.get("name") or reader.get("card_no") or "",
            "actor_role": "reader",
        }

    return {"actor_type": "system", "actor_id": None, "actor_name": "", "actor_role": ""}


def _clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit] + "…"


def _detail_json(detail: Any) -> str:
    if detail is None:
        return ""
    if isinstance(detail, str):
        return _clip(detail, MAX_DETAIL_CHARS)
    try:
        return _clip(json.dumps(detail, ensure_ascii=False, default=str), MAX_DETAIL_CHARS)
    except (TypeError, ValueError):
        return _clip(str(detail), MAX_DETAIL_CHARS)


def log(
    action: str,
    *,
    entity: str = "",
    entity_id: Any = None,
    summary: str = "",
    detail: Any = None,
    status: str = "ok",
    actor: Mapping[str, Any] | None = None,
) -> None:
    """写一条操作日志。任何异常都被吞掉——审计永远不能影响主业务。"""
    info = dict(actor) if actor else current_actor()
    text = summary or action_label(action)
    try:
        db.execute(
            "INSERT INTO operation_logs "
            "(actor_type, actor_id, actor_name, actor_role, action, entity, entity_id,"
            " summary, detail, status, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                str(info.get("actor_type") or "system"),
                info.get("actor_id"),
                str(info.get("actor_name") or ""),
                str(info.get("actor_role") or ""),
                action,
                entity,
                "" if entity_id in (None, "") else str(entity_id),
                _clip(text, MAX_SUMMARY_CHARS),
                _detail_json(detail),
                status,
                _client_ip(),
            ],
        )
    except Exception:  # noqa: BLE001 - 见模块 docstring 第 1 条
        logger.warning("写入操作日志失败（已忽略）：%s", action, exc_info=True)


# ---------------------------------------------------------------- 装饰器


def _json_payload(response: Any) -> dict[str, Any] | None:
    """从视图返回值里取响应体（兼容 `(response, status)` 元组）。"""
    if isinstance(response, tuple):
        response = response[0] if response else None
    getter = getattr(response, "get_json", None)
    if getter is None:
        return None
    try:
        data = getter(silent=True)
    except Exception:  # noqa: BLE001
        return None
    return data if isinstance(data, dict) else None


def _status_of(response: Any) -> int:
    if isinstance(response, tuple) and len(response) > 1 and isinstance(response[1], int):
        return response[1]
    code = getattr(response, "status_code", 200)
    return int(code or 200)


def _entity_id(kwargs: Mapping[str, Any], payload: Mapping[str, Any] | None, entity: str) -> Any:
    """目标记录 id：先看路径参数，再看响应体（新建时 id 在响应里，删除时在 deleted 字段）。"""
    for key in (f"{entity}_id", "id"):
        value = kwargs.get(key)
        if value not in (None, ""):
            return value
    if payload:
        for key in ("id", f"{entity}_id", "deleted"):
            value = payload.get(key)
            if value not in (None, ""):
                return value
    return ""


def audit_action(
    action: str,
    *,
    entity: str = "",
    summary_of: Callable[[Mapping[str, Any], Mapping[str, Any] | None], str] | None = None,
    detail_of: Callable[[Mapping[str, Any], Mapping[str, Any] | None], Any] | None = None,
) -> Callable[[Callable], Callable]:
    """视图装饰器：视图成功（2xx）返回后写一条操作日志。

    失败（4xx/5xx）不写——错误已经由全局处理器记录，这里只关心"改成了什么"。
    ``summary_of`` 可以按路径参数与响应体拼一句更具体的话，例如「下架《三体》」。
    """

    def deco(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            response = view(*args, **kwargs)
            if 200 <= _status_of(response) < 300:
                payload = _json_payload(response)
                try:
                    summary = summary_of(kwargs, payload) if summary_of else ""
                    detail = detail_of(kwargs, payload) if detail_of else None
                except Exception:  # noqa: BLE001 - 摘要生成出错不该影响主流程
                    summary, detail = "", None
                log(
                    action,
                    entity=entity,
                    entity_id=_entity_id(kwargs, payload, entity),
                    summary=summary,
                    detail=detail,
                )
            return response

        return wrapper

    return deco
