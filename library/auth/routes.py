"""登录 / 登出 / 会话信息接口。"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

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
    if service.current_admin() is not None:
        from flask import redirect, url_for

        return redirect(url_for("admin_console"))
    cfg = get_config()
    return render_template(
        "login.html",
        library_name=cfg.library_name,
        library_slogan=cfg.library_slogan,
    )


@bp.post("/api/auth/login")
def login():
    payload = request.get_json(silent=True) or {}
    result = service.login(payload, _client_ip())
    return jsonify(result)


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
