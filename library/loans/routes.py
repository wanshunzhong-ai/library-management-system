"""借阅 REST 接口。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library.loans import service

bp = Blueprint("loans", __name__, url_prefix="/api/loans")


@bp.get("")
def list_loans():
    return jsonify(service.list_loans(request.args.to_dict(flat=True)))


@bp.get("/<int:loan_id>")
def get_loan(loan_id: int):
    return jsonify(service.get_loan(loan_id))


@bp.post("/borrow")
def borrow():
    return jsonify(service.borrow(request.get_json(silent=True) or {})), 201


@bp.post("/return-batch")
def return_batch():
    """批量还书：一次归还多册，逐条返回结果（部分失败不影响其余）。"""
    return jsonify(service.return_batch(request.get_json(silent=True) or {}))


@bp.post("/<int:loan_id>/return")
def return_book(loan_id: int):
    return jsonify(service.return_book(loan_id))


@bp.post("/<int:loan_id>/renew")
def renew(loan_id: int):
    return jsonify(service.renew(loan_id))
