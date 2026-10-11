"""统一登录接口与会话信息接口。

登录入口只有首页 `/` 一处：`/login` 仅作兼容跳转，不再单独渲染登录页。
"""

from __future__ import annotations

from urllib.parse import quote

from flask import Blueprint, jsonify, redirect, request, url_for

from config import get_config
from library import audit
from library.auth import service
from library.auth.permissions import capabilities_payload, role_label

bp = Blueprint("auth", __name__)


@bp.get("/login")
def login_page():
    """兼容旧链接：登录入口已统一到首页。"""
    target = url_for("public.home")
    nxt = (request.args.get("next") or "").strip()
    # 只透传站内相对路径，避免开放重定向
    if nxt.startswith("/") and not nxt.startswith("//"):
        target = f"{target}?next={quote(nxt)}"
    return redirect(target)


def _admin_actor(admin: dict) -> dict:
    return {
        "actor_type": "admin",
        "actor_id": admin.get("id"),
        "actor_name": admin.get("display_name") or admin.get("username") or "",
        "actor_role": admin.get("role") or "",
    }


@bp.post("/api/auth/login")
def login():
    """统一登录：一份账号密码，自动识别管理员或读者。"""
    payload = request.get_json(silent=True) or {}
    result = service.login_unified(payload, service.client_ip())
    if result.get("role") == "admin":
        admin = result.get("admin") or {}
        # 登录本身也是审计事件：需要能看到"谁在什么时候进了后台"
        audit.log(
            "auth.login",
            entity="account",
            entity_id=admin.get("id"),
            summary=f"{role_label(str(admin.get('role') or ''))}「{admin.get('display_name') or admin.get('username')}」登录后台",
            actor=_admin_actor(admin),
        )
    return jsonify(result)


@bp.post("/api/auth/logout")
def logout():
    admin = service.current_admin()
    service.logout()
    if admin:
        audit.log(
            "auth.logout",
            entity="account",
            entity_id=admin.get("id"),
            summary=f"「{admin.get('display_name') or admin.get('username')}」退出登录",
            actor=_admin_actor(admin),
        )
    return jsonify({"ok": True})


@bp.get("/api/auth/me")
def me():
    admin = service.current_admin()
    if admin is None:
        return jsonify({"error": {"code": "unauthorized", "message": "未登录"}}), 401
    cfg = get_config()
    role = str(admin.get("role") or "")
    return jsonify(
        {
            "admin": admin,
            "role_label": role_label(role),
            # 前端据此隐藏/禁用无权入口；后端仍然独立校验，前端只是"不给出入口"
            "permissions": capabilities_payload(role),
            "csrf_token": service.csrf_token(),
            "library": {"name": cfg.library_name, "slogan": cfg.library_slogan},
            "security": service.security_status(admin),
            # 会话信息：前端据此在到期前给出「即将过期」提醒，避免弹窗里未保存的表单无声丢失。
            # refresh_on_request=True 表示后端是滑动续期（任意请求都会重置 Cookie 有效期），
            # 因此前端按「最后一次成功请求」起算闲置时长即可。
            "session": {
                "lifetime_seconds": int(service.session_lifetime().total_seconds()),
                "refresh_on_request": True,
            },
            # 借阅策略：侧栏在任意视图刷新后都能显示，不必先进概览
            "policy": {
                "loan_days": cfg.loan_days,
                "max_renew": cfg.max_renew,
                "fine_per_day": cfg.fine_per_day,
                "fine_max_days": cfg.fine_max_days,
                "fine_cap": cfg.fine_cap,
            },
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
    audit.log(
        "auth.password",
        entity="account",
        entity_id=admin.get("id"),
        summary=f"「{admin.get('display_name') or admin.get('username')}」修改了自己的登录密码",
        actor=_admin_actor(admin),
    )
    service.logout()
    return jsonify({"ok": True, "message": "密码已修改，请使用新密码重新登录"})
