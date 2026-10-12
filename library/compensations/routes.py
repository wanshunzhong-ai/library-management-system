"""赔偿处理 REST 接口（需 `loan:write`）。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.auth.service import current_admin
from library.compensations import service

bp = Blueprint("compensations", __name__, url_prefix="/api/compensations")


def _operator() -> str:
    admin = current_admin()
    if not admin:
        return ""
    return str(admin.get("display_name") or admin.get("username") or "")


def _register_summary(_kwargs: dict, payload: dict | None) -> str:
    data = payload or {}
    kind = service.KIND_LABELS.get(str(data.get("kind") or ""), "赔偿")
    return f"登记{kind} ¥{float(data.get('amount') or 0):.2f}"


@bp.get("")
@require_permission("loan:read")
def list_compensations():
    return jsonify(service.list_compensations(request.args.to_dict(flat=True)))


@bp.get("/<int:comp_id>")
@require_permission("loan:read")
def get_compensation(comp_id: int):
    return jsonify(service.get_compensation(comp_id))


@bp.post("")
@require_permission("loan:write")
@audit.audit_action("compensation.register", entity="compensation", summary_of=_register_summary)
def register():
    return jsonify(service.register(request.get_json(silent=True) or {}, operator=_operator())), 201


@bp.post("/<int:comp_id>/settle")
@require_permission("loan:write")
@audit.audit_action(
    "compensation.settle",
    entity="compensation",
    summary_of=lambda _kw, p: f"赔偿结案，实收 ¥{float((p or {}).get('paid_amount') or 0):.2f}",
)
def settle(comp_id: int):
    return jsonify(service.settle(comp_id, request.get_json(silent=True) or {}, operator=_operator()))


@bp.post("/<int:comp_id>/cancel")
@require_permission("loan:write")
@audit.audit_action("compensation.cancel", entity="compensation", summary_of=lambda _kw, _p: "注销赔偿单")
def cancel(comp_id: int):
    return jsonify(service.cancel(comp_id, request.get_json(silent=True) or {}, operator=_operator()))
