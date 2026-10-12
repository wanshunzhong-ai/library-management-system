"""办证申请的馆员侧接口：审核列表、通过发证、驳回。

权限复用 ``member:write``——"审核通过"这个动作的实质就是建一份读者档案。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.applications import service
from library.auth.permissions import require_permission

bp = Blueprint("applications", __name__, url_prefix="/api/applications")


def _operator() -> str:
    from library.auth.service import current_admin

    admin = current_admin() or {}
    return str(admin.get("display_name") or admin.get("username") or "")


def _applicant(payload) -> str:
    data = payload or {}
    person = (data.get("application") or {}).get("name") or (data.get("member") or {}).get("name") or ""
    return f"「{person}」" if person else ""


def _approve_summary(_kw, payload) -> str:
    card = (payload or {}).get("card_no") or ""
    return f"审核通过并发证{_applicant(payload)}，读者证号 {card}"


def _reject_summary(_kw, payload) -> str:
    return f"驳回办证申请{_applicant(payload)}"


@bp.get("")
@require_permission("member:read")
def list_applications():
    return jsonify(service.list_applications(request.args.to_dict(flat=True)))


@bp.get("/<int:application_id>")
@require_permission("member:read")
def get_application(application_id: int):
    return jsonify(service.get_application(application_id))


@bp.post("/<int:application_id>/approve")
@require_permission("member:write")
@audit.audit_action("application.approve", entity="application", summary_of=_approve_summary)
def approve(application_id: int):
    payload = request.get_json(silent=True) or {}
    return jsonify(service.approve(application_id, payload, operator=_operator()))


@bp.post("/<int:application_id>/reject")
@require_permission("member:write")
@audit.audit_action("application.reject", entity="application", summary_of=_reject_summary)
def reject(application_id: int):
    payload = request.get_json(silent=True) or {}
    reason = str(payload.get("reason") or "")
    return jsonify(service.reject(application_id, reason, operator=_operator()))
