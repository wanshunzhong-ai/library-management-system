"""借阅 REST 接口。"""

from __future__ import annotations

from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.loans import service

bp = Blueprint("loans", __name__, url_prefix="/api/loans")


def _pair(payload: Mapping[str, Any] | None) -> str:
    """借阅记录的一句话描述：书名 + 读者名。"""
    data: Mapping[str, Any] = payload or {}
    title = data.get("book_title") or ""
    reader = data.get("member_name") or ""
    return f"《{title}》" + (f"（读者 {reader}）" if reader else "")


def _batch_summary(_kwargs: Mapping[str, Any], payload: Mapping[str, Any] | None) -> str:
    data: Mapping[str, Any] = payload or {}
    return (
        f"批量还书：请求 {data.get('requested', 0)} 册，"
        f"成功 {data.get('succeeded_count', 0)}、失败 {data.get('failed_count', 0)}，"
        f"罚金合计 ¥{float(data.get('fine_total') or 0):.2f}"
    )


@bp.get("")
def list_loans():
    return jsonify(service.list_loans(request.args.to_dict(flat=True)))


@bp.get("/<int:loan_id>")
def get_loan(loan_id: int):
    return jsonify(service.get_loan(loan_id))


@bp.post("/borrow")
@require_permission("loan:write")
@audit.audit_action("loan.borrow", entity="loan", summary_of=lambda _kw, p: f"办理借书：{_pair(p)}")
def borrow():
    return jsonify(service.borrow(request.get_json(silent=True) or {})), 201


@bp.post("/return-batch")
@require_permission("loan:write")
@audit.audit_action("loan.return_batch", entity="loan", summary_of=_batch_summary)
def return_batch():
    """批量还书：一次归还多册，逐条返回结果（部分失败不影响其余）。"""
    return jsonify(service.return_batch(request.get_json(silent=True) or {}))


@bp.post("/<int:loan_id>/return")
@require_permission("loan:write")
@audit.audit_action("loan.return", entity="loan", summary_of=lambda _kw, p: f"办理还书：{_pair(p)}")
def return_book(loan_id: int):
    return jsonify(service.return_book(loan_id))


@bp.post("/<int:loan_id>/renew")
@require_permission("loan:write")
@audit.audit_action("loan.renew", entity="loan", summary_of=lambda _kw, p: f"续借：{_pair(p)}")
def renew(loan_id: int):
    return jsonify(service.renew(loan_id))
