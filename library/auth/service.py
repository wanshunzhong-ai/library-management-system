"""管理员鉴权：密码哈希、登录限流、会话管理、访问装饰器、CSRF。"""

from __future__ import annotations

import functools
import hmac
import logging
import secrets
import threading
import time
from datetime import timedelta
from typing import Any, Callable

from flask import g, jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from config import get_config
from library.auth import repository as repo
from library.errors import BusinessRuleError, UnauthorizedError, ValidationError

logger = logging.getLogger(__name__)

SESSION_ADMIN_ID = "admin_id"
SESSION_CSRF = "csrf_token"
SESSION_USERNAME = "admin_username"

# 登录失败计数（进程内），key = username|ip
_attempts: dict[str, list] = {}
_attempts_lock = threading.Lock()

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
# 无需登录即可访问的接口前缀
PUBLIC_API_PREFIXES = ("/api/public/",)
# 无需登录即可访问的具体接口
PUBLIC_API_ENDPOINTS = {"auth.login", "auth.me", "auth.logout"}


# ---------------------------------------------------------------- 密码


def hash_password(raw: str) -> str:
    return generate_password_hash(raw, method="pbkdf2:sha256", salt_length=16)


def verify_password(raw: str, hashed: str) -> bool:
    if not hashed:
        return False
    return check_password_hash(hashed, raw)


# ---------------------------------------------------------------- 限流


def _attempt_key(username: str, ip: str) -> str:
    return f"{(username or '').strip().lower()}|{ip or '-'}"


def _check_locked(key: str) -> int:
    """返回剩余锁定秒数，0 表示未锁定。"""
    with _attempts_lock:
        record = _attempts.get(key)
        if not record:
            return 0
        locked_until = record[1]
        if locked_until and locked_until > time.time():
            return int(locked_until - time.time()) + 1
        if locked_until and locked_until <= time.time():
            _attempts.pop(key, None)
    return 0


def _record_failure(key: str) -> None:
    cfg = get_config()
    with _attempts_lock:
        record = _attempts.setdefault(key, [0, 0.0])
        record[0] += 1
        if record[0] >= cfg.login_max_attempts:
            record[1] = time.time() + cfg.login_lock_minutes * 60
            record[0] = 0
            logger.warning("登录失败次数过多，已临时锁定", extra={"extra_fields": {"key": key.split("|")[0]}})


def _clear_failures(key: str) -> None:
    with _attempts_lock:
        _attempts.pop(key, None)


# ---------------------------------------------------------------- 登录 / 登出


def _public_admin(admin: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": admin["id"],
        "username": admin["username"],
        "display_name": admin["display_name"] or admin["username"],
        "role": admin["role"],
        "last_login_at": admin["last_login_at"],
    }


def login(payload: dict[str, Any], ip: str) -> dict[str, Any]:
    cfg = get_config()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    if not username:
        raise ValidationError("请输入用户名", details={"field": "username"})
    if not password:
        raise ValidationError("请输入密码", details={"field": "password"})

    key = _attempt_key(username, ip)
    remaining = _check_locked(key)
    if remaining > 0:
        raise BusinessRuleError(f"登录失败次数过多，请 {remaining} 秒后重试")

    admin = repo.get_by_username(username)
    if admin is None or not verify_password(password, admin["password_hash"]):
        _record_failure(key)
        logger.warning("登录失败", extra={"extra_fields": {"username": username, "ip": ip}})
        raise UnauthorizedError("用户名或密码不正确", details={"field": "password"})
    if not int(admin["is_active"]):
        raise BusinessRuleError("该账号已被停用，请联系系统管理员")

    _clear_failures(key)
    repo.touch_login(admin["id"])
    admin = repo.get(admin["id"]) or admin

    session.clear()
    session.permanent = True
    session[SESSION_ADMIN_ID] = admin["id"]
    session[SESSION_USERNAME] = admin["username"]
    session[SESSION_CSRF] = secrets.token_urlsafe(24)

    logger.info("管理员登录成功", extra={"extra_fields": {"username": admin["username"], "ip": ip}})
    return {"admin": _public_admin(admin), "csrf_token": session[SESSION_CSRF]}


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


def _wants_json() -> bool:
    return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"


def admin_required(view: Callable) -> Callable:
    """后台页面：未登录跳转登录页；接口：返回 401 JSON。"""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_admin() is not None:
            return view(*args, **kwargs)
        if _wants_json():
            return (
                jsonify({"error": {"code": "unauthorized", "message": "登录状态已失效，请重新登录"}}),
                401,
            )
        return redirect(url_for("auth.login_page", next=request.full_path if request.query_string else request.path))

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
