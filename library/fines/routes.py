"""滞纳金的馆员侧接口：缴纳流水查询、代收、减免。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.auth.service import current_admin
from library.fines import service

bp = Blueprint("fines", __name__, url_prefix="/api/fines")


def _operator() -> str:
    admin = current_admin()
    if not admin:
        return ""
    return str(admin.get("display_name") or admin.get("username") or "")


@bp.get("")
@require_permission("loan:write")
def list_payments():
    return jsonify(service.list_payments(request.args.to_dict(flat=True)))


@bp.get("/member/<int:member_id>")
@require_permission("loan:write")
def member_outstanding(member_id: int):
    return jsonify(service.member_outstanding(member_id))


@bp.post("/collect")
@require_permission("loan:write")
@audit.audit_action(
    "fine.collect", entity="member", summary_of=lambda _kw, p: f"收取滞纳金 ¥{float((p or {}).get('amount') or 0):.2f}"
)
def collect():
    result = service.settle(request.get_json(silent=True) or {}, kind="payment", operator=_operator())
    return jsonify(result)


@bp.post("/waive")
@require_permission("loan:write")
@audit.audit_action(
    "fine.waive", entity="member", summary_of=lambda _kw, p: f"减免滞纳金 ¥{float((p or {}).get('amount') or 0):.2f}"
)
def waive():
    result = service.settle(request.get_json(silent=True) or {}, kind="waiver", operator=_operator())
    return jsonify(result)
