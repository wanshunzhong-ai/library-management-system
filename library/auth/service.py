"""统一的登录鉴权：账号密码校验、失败限流、会话管理、访问装饰器、CSRF。

登录入口只有一个（首页 `/`）：用户只填一份账号与密码，服务端按顺序识别身份——
先匹配后台管理员的工作账号，再匹配读者的借书证号 / 手机号，命中哪一类就建立
哪一类会话。管理员与读者共用同一套失败限流，防止交替调用入口绕过锁定。

同一浏览器同时只保留一种身份：任何一次登录都会先清空既有会话再写入新身份，
避免出现"既是管理员又是读者"的歧义。
"""

from __future__ import annotations

import functools
import hmac
import ipaddress
import logging
import secrets
from datetime import timedelta
from typing import Any, Callable, Mapping

from flask import g, jsonify, redirect, request, session, url_for

from config import get_config
from library.auth import repository as repo
from library.auth.passwords import hash_password, verify_password  # noqa: F401  对外沿用旧入口
from library.auth.throttle import (
    clear_login_locks,
    login_throttle,  # noqa: F401  对外沿用旧入口（测试与读者端都从这里取）
    record_login_failure,
    remaining_login_lock,
)
from library.errors import BusinessRuleError, UnauthorizedError, ValidationError

logger = logging.getLogger(__name__)

SESSION_ADMIN_ID = "admin_id"
SESSION_CSRF = "csrf_token"
SESSION_USERNAME = "admin_username"

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# 无需登录即可访问的接口前缀
PUBLIC_API_PREFIXES = ("/api/public/",)
# 无需登录即可访问的具体接口
PUBLIC_API_ENDPOINTS = {"auth.login", "auth.me", "auth.logout"}


# ---------------------------------------------------------------- 登录 / 登出


def _public_admin(admin: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": admin["id"],
        "username": admin["username"],
        "display_name": admin["display_name"] or admin["username"],
        "role": admin["role"],
        "last_login_at": admin["last_login_at"],
    }


def _start_admin_session(admin: dict[str, Any]) -> str:
    """建立管理员会话（单一身份：先清空既有身份）。"""
    session.clear()
    session.permanent = True
    session[SESSION_ADMIN_ID] = admin["id"]
    session[SESSION_USERNAME] = admin["username"]
    session[SESSION_CSRF] = secrets.token_urlsafe(24)
    return session[SESSION_CSRF]


def _reject_login(identifier: str, ip: str, kind: str) -> None:
    """统一失败出口：计数 + 日志 + 同一条不泄露账号是否存在的提示。"""
    record_login_failure(identifier, ip)
    logger.warning(
        "登录失败",
        extra={"extra_fields": {"account": identifier, "ip": ip, "kind": kind}},
    )
    raise UnauthorizedError("账号或密码不正确", details={"field": "password"})


def login_unified(payload: dict[str, Any], ip: str) -> dict[str, Any]:
    """统一登录：一份账号密码，自动识别管理员或读者身份。

    账号字段接受 identifier / username / account 中任意一个。
    返回 {"role", "redirect", "admin"|"member", "csrf_token"}，前端据此跳转。
    """
    identifier = str(
        payload.get("identifier") or payload.get("username") or payload.get("account") or ""
    ).strip()
    password = str(payload.get("password") or "")
    if not identifier:
        raise ValidationError("请输入账号", details={"field": "identifier"})
    if not password:
        raise ValidationError("请输入密码", details={"field": "password"})

    remaining = remaining_login_lock(identifier, ip)
    if remaining > 0:
        raise BusinessRuleError(f"登录失败次数过多，请 {remaining} 秒后重试")

    # 1) 先按后台管理员用户名匹配
    admin = repo.get_by_username(identifier)
    if admin is not None:
        if not verify_password(password, admin["password_hash"]):
            _reject_login(identifier, ip, "admin")
        if not int(admin["is_active"]):
            raise BusinessRuleError("该账号已被停用，请联系系统管理员")
        clear_login_locks(identifier, ip)
        repo.touch_login(admin["id"])
        admin = repo.get(admin["id"]) or admin
        csrf = _start_admin_session(admin)
        logger.info(
            "管理员登录成功", extra={"extra_fields": {"username": admin["username"], "ip": ip}}
        )
        return {
            "role": "admin",
            "redirect": "/admin",
            "admin": _public_admin(admin),
            "csrf_token": csrf,
        }

    # 2) 再按读者借书证号 / 手机号匹配
    from library.reader import service as reader_service  # noqa: PLC0415 - 延迟导入避免循环依赖

    member = reader_service.find_for_auth(identifier)
    if member is not None:
        if not verify_password(password, str(member.get("password_hash") or "")):
            _reject_login(identifier, ip, "reader")
        clear_login_locks(identifier, ip)
        csrf = reader_service.start_session(member)
        logger.info(
            "读者登录成功", extra={"extra_fields": {"card_no": member["card_no"], "ip": ip}}
        )
        return {
            "role": "reader",
            "redirect": "/reader",
            "member": reader_service.public_member(member),
            "csrf_token": csrf,
        }

    _reject_login(identifier, ip, "unknown")


def logout() -> None:
    session.clear()


def current_admin() -> dict[str, Any] | None:
    """从会话取当前管理员（请求内缓存）。"""
    if getattr(g, "_current_admin_loaded", False):
        return getattr(g, "_current_admin", None)
    g._current_admin_loaded = True
    g._current_admin = None

    admin_id = session.get(SESSION_ADMIN_ID)
    if not admin_id:
        return None
    admin = repo.get(int(admin_id))
    if admin is None or not int(admin["is_active"]):
        session.clear()
        return None
    g._current_admin = _public_admin(admin)
    return g._current_admin


def csrf_token() -> str:
    token = session.get(SESSION_CSRF)
    if not token:
        token = secrets.token_urlsafe(24)
        session[SESSION_CSRF] = token
    return token


# ---------------------------------------------------------------- 装饰器


def _trusted_networks() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """解析配置里的受信代理（IP 或 CIDR），带缓存。"""
    cfg = get_config()
    raw = tuple(cfg.trusted_proxies)
    cache = getattr(_trusted_networks, "_cache", None)
    if cache is not None and cache[0] == raw:
        return cache[1]

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for item in raw:
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("LIBRARY_TRUSTED_PROXIES 条目无法解析，已忽略：%s", item)
    _trusted_networks._cache = (raw, tuple(networks))  # type: ignore[attr-defined]
    return _trusted_networks._cache[1]  # type: ignore[attr-defined]


def _is_trusted_proxy(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for network in _trusted_networks():
        try:
            if addr in network:
                return True
        except TypeError:  # IPv4 / IPv6 版本不一致
            continue
    return False


def client_ip() -> str:
    """取真实客户端 IP。

    只有请求确实来自受信代理（``LIBRARY_TRUSTED_PROXIES``）时才采信
    ``X-Forwarded-For``，且在代理链中从右向左取第一个非受信地址 ——
    否则任意客户端都能靠伪造请求头换一个新的限流桶，登录锁定形同虚设。
    """
    remote = (request.remote_addr or "-").strip()
    if not _is_trusted_proxy(remote):
        return remote

    forwarded = request.headers.get("X-Forwarded-For", "")
    chain = [part.strip() for part in forwarded.split(",") if part.strip()]
    if not chain:
        return remote
    for hop in reversed(chain):
        if not _is_trusted_proxy(hop):
            return hop
    return chain[0]


def admin_password_is_default(admin: Mapping[str, Any]) -> bool:
    """该管理员账号的密码是否仍是配置里的初始口令。"""
    cfg = get_config()
    if not cfg.admin_password:
        return False
    return verify_password(cfg.admin_password, str(admin.get("password_hash") or ""))


def admins_using_default_password() -> list[str]:
    """仍在使用初始口令的管理员用户名列表（启动自检用）。"""
    cfg = get_config()
    if not cfg.admin_password:
        return []
    return [
        str(row["username"])
        for row in repo.list_all()
        if verify_password(cfg.admin_password, str(row.get("password_hash") or ""))
    ]


def security_status(admin: Mapping[str, Any]) -> dict[str, Any]:
    """后台首页的安全提示信号（前端据此显示醒目横幅）。"""
    row = repo.get(int(admin["id"])) or {}
    return {"default_admin_password": admin_password_is_default(row)}


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def admin_required(view: Callable) -> Callable:
    """后台页面：未登录跳统一登录页（首页）；接口：返回 401 JSON。"""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_admin() is not None:
            return view(*args, **kwargs)
        if _wants_json():
            return (
                jsonify({"error": {"code": "unauthorized", "message": "登录状态已失效，请重新登录"}}),
                401,
            )
        nxt = request.full_path if request.query_string else request.path
        return redirect(url_for("public.home", next=nxt))

    return wrapper


def csrf_protect() -> tuple | None:
    """写操作 CSRF 校验（同源双提交 token）。返回响应表示校验失败。"""
    if request.method in SAFE_METHODS:
        return None
    endpoint = request.endpoint or ""
    if endpoint in PUBLIC_API_ENDPOINTS:
        return None

    expected = session.get(SESSION_CSRF)
    provided = request.headers.get("X-CSRF-Token") or (request.get_json(silent=True) or {}).get("_csrf")
    if not expected or not provided or not hmac.compare_digest(str(expected), str(provided)):
        logger.warning("CSRF 校验失败", extra={"extra_fields": {"path": request.path}})
        return (
            jsonify({"error": {"code": "csrf_failed", "message": "请求校验失败，请刷新页面后重试"}}),
            403,
        )
    return None


# ---------------------------------------------------------------- 初始化


def ensure_default_admin() -> bool:
    """库中没有管理员时，用配置创建默认账号。"""
    if repo.count() > 0:
        return False
    cfg = get_config()
    if not cfg.admin_password:
        raise ValidationError("必须通过 LIBRARY_ADMIN_PASSWORD 提供初始管理员密码")
    repo.create(cfg.admin_user, hash_password(cfg.admin_password), "系统管理员", "admin")
    logger.warning(
        "已创建默认管理员账号，请尽快登录后修改密码",
        extra={"extra_fields": {"username": cfg.admin_user}},
    )
    return True


def change_password(admin_id: int, old_password: str, new_password: str) -> None:
    admin = repo.get(admin_id)
    if admin is None:
        raise ValidationError("账号不存在")
    if not verify_password(old_password, admin["password_hash"]):
        raise ValidationError("当前密码不正确", details={"field": "old_password"})
    if len(new_password) < 6:
        raise ValidationError("新密码至少 6 位", details={"field": "new_password"})
    if new_password == old_password:
        raise ValidationError("新密码不能与当前密码相同", details={"field": "new_password"})
    repo.update_password(admin_id, hash_password(new_password))
    logger.info("管理员修改密码", extra={"extra_fields": {"admin_id": admin_id}})


def session_lifetime() -> timedelta:
    return timedelta(hours=get_config().session_hours)
