"""预约的馆员侧接口：查询队列、通知到书、取消。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.reservations import service

bp = Blueprint("reservations", __name__, url_prefix="/api/reservations")


def _summary(payload, verb: str) -> str:
    data = payload or {}
    title = data.get("book_title") or ""
    reader = data.get("member_name") or ""
    tail = f"（读者 {reader}）" if reader else ""
    return f"{verb}预约：《{title}》{tail}"


@bp.get("")
@require_permission("loan:read")
def list_reservations():
    return jsonify(service.list_reservations(request.args.to_dict(flat=True)))


@bp.post("/<int:res_id>/ready")
@require_permission("loan:write")
@audit.audit_action("reservation.ready", entity="reservation", summary_of=lambda _kw, p: _summary(p, "通知到书"))
def mark_ready(res_id: int):
    return jsonify(service.mark_ready(res_id))


@bp.post("/<int:res_id>/cancel")
@require_permission("loan:write")
@audit.audit_action("reservation.cancel", entity="reservation", summary_of=lambda _kw, p: _summary(p, "取消"))
def cancel(res_id: int):
    return jsonify(service.cancel_reservation(res_id))
