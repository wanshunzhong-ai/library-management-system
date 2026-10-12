"""采编验收 REST 接口（需 `book:write`）。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.acquisitions import service
from library.auth.permissions import require_permission
from library.auth.service import current_admin

bp = Blueprint("acquisitions", __name__, url_prefix="/api/acquisitions")


def _operator() -> str:
    admin = current_admin()
    if not admin:
        return ""
    return str(admin.get("display_name") or admin.get("username") or "")


_title = lambda p: str((p or {}).get("title") or "")


@bp.get("")
@require_permission("book:read")
def list_acquisitions():
    return jsonify(service.list_acquisitions(request.args.to_dict(flat=True)))


@bp.get("/<int:acq_id>")
@require_permission("book:read")
def get_acquisition(acq_id: int):
    return jsonify(service.get_acquisition(acq_id))


@bp.post("")
@require_permission("book:write")
@audit.audit_action(
    "acquisition.register",
    entity="acquisition",
    summary_of=lambda _kw, p: f"登记采编验收单《{_title(p)}》{int((p or {}).get('copies') or 0)} 册",
)
def register():
    return jsonify(service.register(request.get_json(silent=True) or {}, operator=_operator())), 201


@bp.post("/<int:acq_id>/accept")
@require_permission("book:write")
@audit.audit_action(
    "acquisition.accept",
    entity="acquisition",
    summary_of=lambda _kw, p: f"采编验收《{_title(p)}》入库 {int((p or {}).get('added_copies') or 0)} 册",
)
def accept(acq_id: int):
    return jsonify(service.accept(acq_id, request.get_json(silent=True) or {}, operator=_operator()))


@bp.post("/<int:acq_id>/reject")
@require_permission("book:write")
@audit.audit_action(
    "acquisition.reject", entity="acquisition", summary_of=lambda _kw, p: f"采编验收驳回《{_title(p)}》"
)
def reject(acq_id: int):
    return jsonify(service.reject(acq_id, request.get_json(silent=True) or {}, operator=_operator()))


@bp.delete("/<int:acq_id>")
@require_permission("book:write")
@audit.audit_action(
    "acquisition.delete", entity="acquisition", summary_of=lambda _kw, p: f"删除验收单《{_title(p)}》"
)
def delete_acquisition(acq_id: int):
    return jsonify(service.delete_acquisition(acq_id))
