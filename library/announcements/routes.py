"""公告的馆员侧接口：增删改查与发布 / 下线。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.announcements import service
from library.auth.permissions import require_permission

bp = Blueprint("announcements", __name__, url_prefix="/api/announcements")


def _operator() -> str:
    from library.auth.service import current_admin

    admin = current_admin() or {}
    return str(admin.get("display_name") or admin.get("username") or "")


def _title_of(_kwargs, payload) -> str:
    return f"公告《{(payload or {}).get('title') or ''}》"


def _status_summary(verb: str):
    def _fn(_kwargs, payload):
        return f"{verb}公告《{(payload or {}).get('title') or ''}》"

    return _fn


@bp.get("")
@require_permission("announcement:read")
def list_announcements():
    return jsonify(service.list_announcements(request.args.to_dict(flat=True)))


@bp.get("/<int:announcement_id>")
@require_permission("announcement:read")
def get_announcement(announcement_id: int):
    return jsonify(service.get_announcement(announcement_id))


@bp.post("")
@require_permission("announcement:write")
@audit.audit_action("announcement.create", entity="announcement", summary_of=_title_of)
def create_announcement():
    return jsonify(service.create_announcement(request.get_json(silent=True) or {}, operator=_operator())), 201


@bp.put("/<int:announcement_id>")
@require_permission("announcement:write")
@audit.audit_action("announcement.update", entity="announcement", summary_of=_title_of)
def update_announcement(announcement_id: int):
    payload = request.get_json(silent=True) or {}
    return jsonify(service.update_announcement(announcement_id, payload, operator=_operator()))


@bp.post("/<int:announcement_id>/status")
@require_permission("announcement:write")
@audit.audit_action(
    "announcement.status", entity="announcement", summary_of=_status_summary("调整状态")
)
def set_status(announcement_id: int):
    payload = request.get_json(silent=True) or {}
    status = str(payload.get("status") or "")
    return jsonify(service.set_status(announcement_id, status, operator=_operator()))


@bp.delete("/<int:announcement_id>")
@require_permission("announcement:write")
@audit.audit_action("announcement.delete", entity="announcement", summary_of=_title_of)
def delete_announcement(announcement_id: int):
    return jsonify(service.delete_announcement(announcement_id))
