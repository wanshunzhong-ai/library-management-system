"""读者门户页面与个人中心接口。"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from config import get_config
from library.auth import service as auth_service
from library.errors import UnauthorizedError
from library.reader import service

bp = Blueprint("reader", __name__)


def _body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


def _require_reader() -> dict[str, Any]:
    """/api/reader/* 已由全局守卫拦截，这里再兜一层，防止守卫被绕过。"""
    member = service.current_reader()
    if member is None:
        raise UnauthorizedError("请先登录读者账号")
    return member


@bp.get("/reader/login")
def login_page():
    """兼容旧链接：登录入口已统一到首页。"""
    return redirect(url_for("public.home"))


@bp.get("/reader")
@service.reader_required
def home():
    cfg = get_config()
    return render_template(
        "reader.html",
        library_name=cfg.library_name,
        library_slogan=cfg.library_slogan,
    )


@bp.post("/api/reader/login")
def login():
    return jsonify(service.login(_body(), auth_service.client_ip()))


@bp.post("/api/reader/logout")
def logout():
    service.logout()
    return jsonify({"ok": True})


@bp.get("/api/reader/me")
def me():
    return jsonify(service.me())


@bp.get("/api/reader/loans")
def my_loans():
    member = _require_reader()
    return jsonify(service.list_my_loans(member, request.args.to_dict(flat=True)))


@bp.post("/api/reader/loans/<int:loan_id>/renew")
def renew(loan_id: int):
    member = _require_reader()
    return jsonify(service.renew_my_loan(member, loan_id))


@bp.put("/api/reader/profile")
def update_profile():
    member = _require_reader()
    return jsonify(service.update_profile(member, _body()))


@bp.post("/api/reader/password")
def change_password():
    member = _require_reader()
    body = _body()
    service.change_password(
        member,
        str(body.get("old_password") or ""),
        str(body.get("new_password") or ""),
    )
    return jsonify({"ok": True, "message": "密码已修改，请使用新密码重新登录"})
