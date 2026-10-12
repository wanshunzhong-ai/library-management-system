"""盘点 REST 接口（需 `book:write`）。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.auth.service import current_admin
from library.stocktaking import service

bp = Blueprint("stocktaking", __name__, url_prefix="/api/stocktaking")


def _operator() -> str:
    admin = current_admin()
    if not admin:
        return ""
    return str(admin.get("display_name") or admin.get("username") or "")


@bp.get("")
@require_permission("book:read")
def list_batches():
    return jsonify(service.list_batches(request.args.to_dict(flat=True)))


@bp.get("/<int:batch_id>")
@require_permission("book:read")
def get_batch(batch_id: int):
    return jsonify(service.get_batch(batch_id, request.args.to_dict(flat=True)))


@bp.post("")
@require_permission("book:write")
@audit.audit_action(
    "stocktaking.create",
    entity="stocktaking",
    summary_of=lambda _kw, p: f"开启盘点单（范围：{(p or {}).get('location') or '全馆'}）",
)
def create_batch():
    return jsonify(service.create_batch(request.get_json(silent=True) or {}, operator=_operator())), 201


@bp.put("/<int:batch_id>/items/<int:item_id>")
@require_permission("book:write")
@audit.audit_action(
    "stocktaking.count",
    entity="stocktaking",
    summary_of=lambda kw, p: (
        f"盘点录入：实盘 {(p or {}).get('counted_copies') if (p or {}).get('counted_copies') not in (None, '') else '—'} 册"
    ),
)
def set_counted(batch_id: int, item_id: int):
    return jsonify(service.set_counted(batch_id, item_id, request.get_json(silent=True) or {}))


@bp.post("/<int:batch_id>/close")
@require_permission("book:write")
@audit.audit_action("stocktaking.close", entity="stocktaking", summary_of=lambda _kw, _p: "关闭盘点单")
def close_batch(batch_id: int):
    return jsonify(service.close_batch(batch_id))


@bp.post("/<int:batch_id>/reopen")
@require_permission("book:write")
@audit.audit_action("stocktaking.reopen", entity="stocktaking", summary_of=lambda _kw, _p: "重新打开盘点单")
def reopen_batch(batch_id: int):
    return jsonify(service.reopen_batch(batch_id))


@bp.post("/<int:batch_id>/apply")
@require_permission("book:write")
@audit.audit_action(
    "stocktaking.apply",
    entity="stocktaking",
    summary_of=lambda _kw, p: f"按盘点差异调账 {int((p or {}).get('applied') or 0)} 种",
)
def apply_batch(batch_id: int):
    return jsonify(service.apply_batch(batch_id, request.get_json(silent=True) or {}))


@bp.delete("/<int:batch_id>")
@require_permission("book:write")
@audit.audit_action("stocktaking.delete", entity="stocktaking", summary_of=lambda _kw, p: f"删除盘点单 {(p or {}).get('code') or ''}")
def delete_batch(batch_id: int):
    return jsonify(service.delete_batch(batch_id))
