"""读者账号：登录、会话、个人借阅查询与自助服务。

读者与管理员使用不同的会话键（session["member_id"] 与 session["admin_id"]）和
各自独立的 CSRF token；登录入口已统一到首页，由鉴权层识别身份后调用本模块
的 start_session 建立读者会话。一次登录只保留一种身份，不会同时存在两套会话。
读者只能访问自己的数据，任何跨读者的读写都按越权处理。
"""

from __future__ import annotations

import functools
import hmac
import logging
import secrets
from typing import Any, Callable, Mapping

from flask import g, jsonify, redirect, request, session, url_for

from config import get_config
from library import db
from library.auth.passwords import hash_password, verify_password
from library.auth.throttle import LoginThrottle, login_throttle
from library.errors import (
    BusinessRuleError,
    ForbiddenError,
    NotFoundError,
    UnauthorizedError,
    ValidationError,
)
from library.loans import repository as loan_repo
from library.loans import service as loan_service
from library.members import repository as member_repo
from library.validation import optional_email, optional_str

logger = logging.getLogger(__name__)

SESSION_MEMBER_ID = "member_id"
SESSION_READER_CSRF = "reader_csrf"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# 读者端免登录接口（登录页与登录动作本身）
PUBLIC_ENDPOINTS = {"reader.login_page", "reader.login"}

# 与管理员共用同一个限流器：登录入口统一，两套计数会给出绕过锁定的空子
reader_throttle = login_throttle


# ---------------------------------------------------------------- 资料视图


def public_member(row: Mapping[str, Any]) -> dict[str, Any]:
    """读者本人可见的资料（不含密码哈希）。"""
    return {
        "id": int(row["id"]),
        "card_no": row["card_no"],
        "name": row["name"],
        "phone": row["phone"],
        "email": row["email"],
        "member_type": row["member_type"],
        "status": row["status"],
        "max_borrow": int(row["max_borrow"]),
        "last_login_at": row.get("last_login_at"),
    }


# ---------------------------------------------------------------- 会话


def find_for_auth(account: str) -> Mapping[str, Any] | None:
    """按借书证号或手机号取鉴权行（供统一登录入口识别身份用）。"""
    key = str(account or "").strip()
    if not key:
        return None
    return member_repo.get_for_auth_by_account(key)


def start_session(row: Mapping[str, Any]) -> str:
    """建立读者会话并返回 CSRF token。

    供统一登录入口与读者专用登录接口共用：先清空既有会话，
    保证同一浏览器一次只持有一种身份（读者或管理员）。
    传入的是带 password_hash 的鉴权行，此处只取 id 并刷新登录时间。
    """
    member_id = int(row["id"])
    member_repo.touch_login(member_id)
    session.clear()
    session.permanent = True
    session[SESSION_MEMBER_ID] = member_id
    session[SESSION_READER_CSRF] = secrets.token_urlsafe(24)
    return session[SESSION_READER_CSRF]


def login(payload: Mapping[str, Any], ip: str) -> dict[str, Any]:
    """读者专用登录接口（保留）：账号可以是借书证号或手机号。"""
    account = str(payload.get("account") or payload.get("card_no") or payload.get("identifier") or "").strip()
    password = str(payload.get("password") or "")
    if not account:
        raise ValidationError("请输入借书证号或手机号", details={"field": "account"})
    if not password:
        raise ValidationError("请输入密码", details={"field": "password"})

    key = LoginThrottle.key_for(account, ip)
    remaining = reader_throttle.remaining_lock(key)
    if remaining > 0:
        raise BusinessRuleError(f"登录失败次数过多，请 {remaining} 秒后重试")

    row = member_repo.get_for_auth_by_account(account)
    if row is None or not verify_password(password, str(row.get("password_hash") or "")):
        reader_throttle.record_failure(key)
        logger.warning("读者登录失败", extra={"extra_fields": {"account": account, "ip": ip}})
        raise UnauthorizedError("账号或密码不正确", details={"field": "password"})

    reader_throttle.clear(key)
    csrf = start_session(row)
    row = member_repo.get(int(row["id"])) or row

    logger.info(
        "读者登录成功",
        extra={"extra_fields": {"card_no": row["card_no"], "ip": ip}},
    )
    return {"member": public_member(row), "csrf_token": csrf}


def logout() -> None:
    session.pop(SESSION_MEMBER_ID, None)
    session.pop(SESSION_READER_CSRF, None)


def current_reader() -> dict[str, Any] | None:
    """取当前登录读者（请求内缓存）。"""
    if getattr(g, "_current_reader_loaded", False):
        return getattr(g, "_current_reader", None)
    g._current_reader_loaded = True
    g._current_reader = None

    member_id = session.get(SESSION_MEMBER_ID)
    if not member_id:
        return None
    row = member_repo.get(int(member_id))
    if row is None:
        session.pop(SESSION_MEMBER_ID, None)
        return None
    g._current_reader = public_member(row)
    return g._current_reader


def csrf_token() -> str:
    token = session.get(SESSION_READER_CSRF)
    if not token:
        token = secrets.token_urlsafe(24)
        session[SESSION_READER_CSRF] = token
    return token


def csrf_protect() -> tuple | None:
    """读者端写操作 CSRF 校验。返回响应表示校验失败。"""
    if request.method in SAFE_METHODS:
        return None
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None

    expected = session.get(SESSION_READER_CSRF)
    provided = request.headers.get("X-CSRF-Token") or (request.get_json(silent=True) or {}).get("_csrf")
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        logger.warning("读者端 CSRF 校验失败", extra={"extra_fields": {"path": request.path}})
        return (
            jsonify({"error": {"code": "csrf_failed", "message": "请求校验失败，请刷新页面后重试"}}),
            403,
        )
    return None


def reader_required(view: Callable) -> Callable:
    """个人中心页面：未登录跳统一登录页（首页）；接口：返回 401 JSON。"""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_reader() is not None:
            return view(*args, **kwargs)
        if request.path.startswith("/api/"):
            return (
                jsonify({"error": {"code": "unauthorized", "message": "登录状态已失效，请重新登录"}}),
                401,
            )
        return redirect(url_for("public.home", next=request.path))

    return wrapper


# ---------------------------------------------------------------- 个人中心数据


def stats(member: Mapping[str, Any]) -> dict[str, Any]:
    """在借 / 逾期 / 历史册数与累计罚金（按日期差 × 日罚金估算）。"""
    cfg = get_config()
    row = db.query_one(
        """
        SELECT
            SUM(CASE WHEN returned_at IS NULL THEN 1 ELSE 0 END) AS active_loans,
            SUM(CASE WHEN returned_at IS NULL AND due_at < datetime('now', 'localtime')
                     THEN 1 ELSE 0 END) AS overdue_loans,
            SUM(CASE WHEN returned_at IS NOT NULL THEN 1 ELSE 0 END) AS returned_total,
            COUNT(*) AS loan_total,
            SUM(CASE WHEN returned_at IS NULL
                     THEN MAX(0, CAST(julianday(date('now', 'localtime')) - julianday(date(due_at)) AS INTEGER))
                     ELSE MAX(0, CAST(julianday(date(returned_at)) - julianday(date(due_at)) AS INTEGER))
                END) AS overdue_days_total
          FROM loans
         WHERE member_id = ?
        """,
        [int(member["id"])],
    ) or {}

    active = int(row.get("active_loans") or 0)
    overdue_days = int(row.get("overdue_days_total") or 0)
    return {
        "active_loans": active,
        "overdue_loans": int(row.get("overdue_loans") or 0),
        "returned_total": int(row.get("returned_total") or 0),
        "loan_total": int(row.get("loan_total") or 0),
        "quota_left": max(0, int(member["max_borrow"]) - active),
        "fine_total": round(overdue_days * cfg.fine_per_day, 2),
        "overdue_days_total": overdue_days,
    }


def me() -> dict[str, Any]:
    member = current_reader()
    if member is None:
        raise UnauthorizedError("请先登录读者账号")
    cfg = get_config()
    return {
        "member": member,
        "stats": stats(member),
        "rules": {
            "loan_days": cfg.loan_days,
            "max_renew": cfg.max_renew,
            "fine_per_day": cfg.fine_per_day,
        },
        "library": {"name": cfg.library_name, "slogan": cfg.library_slogan},
        "csrf_token": csrf_token(),
    }


def list_my_loans(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    """只看自己的借阅记录：member_id 强制取自会话，不接受前端传入。"""
    status = (query.get("status") or "all").strip()
    if status not in {"all", "open", "borrowed", "overdue", "returned"}:
        raise ValidationError(
            "status 只能是 all/open/borrowed/overdue/returned", details={"field": "status"}
        )
    return loan_service.list_loans(
        {
            "status": status,
            "member_id": int(member["id"]),
            "page": query.get("page", 1),
            "page_size": query.get("page_size", 10),
        }
    )


def renew_my_loan(member: Mapping[str, Any], loan_id: int) -> dict[str, Any]:
    """自助续借：先校验记录归属，再复用管理端的续借规则。"""
    raw = loan_repo.raw(loan_id)
    if raw is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    if int(raw["member_id"]) != int(member["id"]):
        logger.warning(
            "读者尝试续借他人记录",
            extra={"extra_fields": {"member_id": member["id"], "loan_id": loan_id}},
        )
        raise ForbiddenError("只能续借本人的借阅记录")
    if member["status"] != "active":
        raise BusinessRuleError("账号已被冻结，暂时无法自助续借，请联系管理员")

    result = loan_service.renew(loan_id)
    logger.info(
        "读者自助续借",
        extra={"extra_fields": {"member_id": member["id"], "loan_id": loan_id}},
    )
    return result


def update_profile(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """读者自助维护联系方式；姓名 / 证号 / 可借上限仍由管理员掌握。"""
    phone = optional_str(payload, "phone", label="手机号", max_length=20)
    email = optional_email(payload, "email")

    # 手机号可用于登录，必须保证不与其它读者冲突
    if phone:
        conflict = db.query_one(
            "SELECT id, name FROM members WHERE phone = ? AND id <> ? LIMIT 1",
            [phone, int(member["id"])],
        )
        if conflict is not None:
            raise BusinessRuleError(
                "该手机号已被其他读者使用，请更换或联系管理员",
                details={"field": "phone"},
            )

    member_repo.update_contact(int(member["id"]), phone, email)
    row = member_repo.get(int(member["id"])) or {}
    return public_member(row)


def change_password(member: Mapping[str, Any], old_password: str, new_password: str) -> None:
    row = member_repo.get_for_auth_by_account(str(member["card_no"]))
    if row is None or not verify_password(old_password, str(row.get("password_hash") or "")):
        raise ValidationError("当前密码不正确", details={"field": "old_password"})
    if len(new_password) < 6:
        raise ValidationError("新密码至少 6 位", details={"field": "new_password"})
    if new_password == old_password:
        raise ValidationError("新密码不能与当前密码相同", details={"field": "new_password"})

    member_repo.update_password_hash(int(member["id"]), hash_password(new_password))
    logout()  # 改密后强制重新登录
    logger.info("读者修改密码", extra={"extra_fields": {"member_id": member["id"]}})
