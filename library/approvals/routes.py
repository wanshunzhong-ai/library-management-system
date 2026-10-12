"""审批工单接口（仅系统管理员）。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.approvals import service
from library.auth.permissions import require_permission
from library.auth.service import current_admin

bp = Blueprint("approvals", __name__, url_prefix="/api/approvals")


def _operator() -> str:
    admin = current_admin()
    if not admin:
        return ""
    return str(admin.get("display_name") or admin.get("username") or "")


@bp.get("")
@require_permission("approval:approve")
def list_approvals():
    return jsonify(service.list_approvals(request.args.to_dict(flat=True)))


@bp.get("/pending-count")
@require_permission("approval:approve")
def pending_count():
    return jsonify({"pending": service.pending_count()})


@bp.post("/<int:approval_id>/approve")
@require_permission("approval:approve")
@audit.audit_action("approval.approve", entity="approval", summary_of=lambda _kw, p: f"通过审批工单 #{_kw.get('approval_id')}")
def approve(approval_id: int):
    result = service.approve(approval_id, request.get_json(silent=True) or {}, operator=_operator())
    return jsonify(result)


@bp.post("/<int:approval_id>/reject")
@require_permission("approval:approve")
@audit.audit_action("approval.reject", entity="approval", summary_of=lambda _kw, p: f"驳回审批工单 #{_kw.get('approval_id')}")
def reject(approval_id: int):
    result = service.reject(approval_id, request.get_json(silent=True) or {}, operator=_operator())
    return jsonify(result)
