"""统一登录接口与会话信息接口。

登录入口只有首页 `/` 一处：`/login` 仅作兼容跳转，不再单独渲染登录页。
"""

from __future__ import annotations

from urllib.parse import quote

from flask import Blueprint, jsonify, redirect, request, url_for

from config import get_config
from library.auth import service

bp = Blueprint("auth", __name__)


def _client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "-"


@bp.get("/login")
def login_page():
    """兼容旧链接：登录入口已统一到首页。"""
    target = url_for("public.home")
    nxt = (request.args.get("next") or "").strip()
    # 只透传站内相对路径，避免开放重定向
    if nxt.startswith("/") and not nxt.startswith("//"):
        target = f"{target}?next={quote(nxt)}"
    return redirect(target)


@bp.post("/api/auth/login")
def login():
    """统一登录：一份账号密码，自动识别管理员或读者。"""
    payload = request.get_json(silent=True) or {}
    return jsonify(service.login_unified(payload, _client_ip()))


@bp.post("/api/auth/logout")
def logout():
    service.logout()
    return jsonify({"ok": True})


@bp.get("/api/auth/me")
def me():
    admin = service.current_admin()
    if admin is None:
        return jsonify({"error": {"code": "unauthorized", "message": "未登录"}}), 401
    cfg = get_config()
    return jsonify(
        {
            "admin": admin,
            "csrf_token": service.csrf_token(),
            "library": {"name": cfg.library_name, "slogan": cfg.library_slogan},
        }
    )


@bp.post("/api/auth/password")
def change_password():
    admin = service.current_admin()
    if admin is None:
        return jsonify({"error": {"code": "unauthorized", "message": "未登录"}}), 401
    payload = request.get_json(silent=True) or {}
    service.change_password(
        admin["id"],
        str(payload.get("old_password") or ""),
        str(payload.get("new_password") or ""),
    )
    service.logout()
    return jsonify({"ok": True, "message": "密码已修改，请使用新密码重新登录"})
