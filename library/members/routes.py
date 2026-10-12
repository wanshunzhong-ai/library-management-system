"""读者 REST 接口。"""

from __future__ import annotations

from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.members import service

bp = Blueprint("members", __name__, url_prefix="/api/members")


def _name_of(payload: Mapping[str, Any] | None) -> str:
    data: Mapping[str, Any] = payload or {}
    return str(data.get("name") or data.get("card_no") or "")


@bp.get("")
@require_permission("member:read")
def list_members():
    return jsonify(service.list_members(request.args.to_dict(flat=True)))


@bp.get("/<int:member_id>")
@require_permission("member:read")
def get_member(member_id: int):
    return jsonify(service.get_member(member_id))


@bp.post("")
@require_permission("member:write")
@audit.audit_action("member.create", entity="member", summary_of=lambda _kw, p: f"新增读者「{_name_of(p)}」")
def create_member():
    return jsonify(service.create_member(request.get_json(silent=True) or {})), 201


@bp.put("/<int:member_id>")
@require_permission("member:write")
@audit.audit_action("member.update", entity="member", summary_of=lambda _kw, p: f"修改读者「{_name_of(p)}」")
def update_member(member_id: int):
    return jsonify(service.update_member(member_id, request.get_json(silent=True) or {}))


@bp.delete("/<int:member_id>")
@require_permission("member:write")
@audit.audit_action(
    "member.delete", entity="member", summary_of=lambda _kw, p: f"删除读者「{_name_of(p)}」"
)
def delete_member(member_id: int):
    # 先取姓名：删除后记录里已没有可读的读者名，审计日志需要它
    member = service.get_member(member_id)
    service.delete_member(member_id)
    return jsonify({"deleted": member_id, "name": member.get("name") or ""})


@bp.post("/<int:member_id>/reset-password")
@require_permission("member:write")
@audit.audit_action(
    "member.reset_password",
    entity="member",
    summary_of=lambda kw, _p: f"重置读者门户密码（读者 #{kw.get('member_id')}）",
)
def reset_password(member_id: int):
    """把读者门户密码重置为初始密码（证号后 6 位）。"""
    return jsonify(service.reset_member_password(member_id))


@bp.post("/<int:member_id>/notify")
@require_permission("member:write")
@audit.audit_action(
    "notification.send",
    entity="member",
    summary_of=lambda kw, p: f"向读者 #{kw.get('member_id')} 发送站内通知「{str((p or {}).get('title') or '')}」",
)
def notify_member(member_id: int):
    """馆员给某位读者发一条站内通知（催还、预约取书提醒等）。

    有它，通知中心才是双向的：既能把系统事件告诉读者，也能让馆员主动说一句话。
    """
    from library.auth import service as auth_service  # noqa: PLC0415
    from library.members import service as member_service  # noqa: PLC0415
    from library.notifications import service as notification_service  # noqa: PLC0415

    member_service.get_member(member_id)  # 读者不存在时先 404，别写出无主通知
    operator = str((auth_service.current_admin() or {}).get("username") or "")
    return jsonify(notification_service.send_system(member_id, request.get_json(silent=True) or {}, operator=operator)), 201
