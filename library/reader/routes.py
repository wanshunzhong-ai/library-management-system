"""读者门户页面与个人中心接口。"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from config import get_config
from library import audit
from library.auth import service as auth_service
from library.errors import UnauthorizedError
from library.reader import service

bp = Blueprint("reader", __name__)


def _body() -> dict[str, Any]:
    return request.get_json(silent=True) or {}


def _require_reader() -> dict[str, Any]:
    """/api/reader/* 已由全局守卫拦截，这里再兜一层，防止守卫被绕过。"""
    member = service.current_reader()
    if member is None:
        raise UnauthorizedError("请先登录读者账号")
    return member


@bp.get("/reader/login")
def login_page():
    """兼容旧链接：登录入口已统一到首页。"""
    return redirect(url_for("public.home"))


@bp.get("/reader")
@service.reader_required
def home():
    cfg = get_config()
    return render_template(
        "reader.html",
        library_name=cfg.library_name,
        library_slogan=cfg.library_slogan,
    )


@bp.post("/api/reader/login")
def login():
    return jsonify(service.login(_body(), auth_service.client_ip()))


@bp.post("/api/reader/logout")
def logout():
    service.logout()
    return jsonify({"ok": True})


@bp.get("/api/reader/me")
def me():
    return jsonify(service.me())


@bp.get("/api/reader/loans")
def my_loans():
    member = _require_reader()
    return jsonify(service.list_my_loans(member, request.args.to_dict(flat=True)))


@bp.post("/api/reader/loans/<int:loan_id>/renew")
def renew(loan_id: int):
    member = _require_reader()
    result = service.renew_my_loan(member, loan_id)
    audit.log(
        "loan.renew",
        entity="loan",
        entity_id=loan_id,
        summary=f"读者自助续借《{result.get('book_title') or ''}》",
    )
    return jsonify(result)


@bp.put("/api/reader/profile")
def update_profile():
    member = _require_reader()
    updated = service.update_profile(member, _body())
    audit.log(
        "reader.profile",
        entity="member",
        entity_id=member["id"],
        summary="读者修改联系方式",
    )
    return jsonify(updated)


@bp.post("/api/reader/password")
def change_password():
    member = _require_reader()
    body = _body()
    service.change_password(
        member,
        str(body.get("old_password") or ""),
        str(body.get("new_password") or ""),
    )
    audit.log("reader.password", entity="member", entity_id=member["id"], summary="读者修改登录密码")
    return jsonify({"ok": True, "message": "密码已修改，请使用新密码重新登录"})


# ---------------------------------------------------------------- 预约


@bp.get("/api/reader/books/search")
def search_books():
    """预约前的馆藏检索（复用门户检索口径）。"""
    _require_reader()
    return jsonify(service.search_books_for_reserve(request.args.to_dict(flat=True)))


@bp.get("/api/reader/reservations")
def my_reservations():
    member = _require_reader()
    return jsonify(service.list_my_reservations(member, request.args.to_dict(flat=True)))


@bp.post("/api/reader/reservations")
def create_reservation():
    member = _require_reader()
    result = service.create_reservation(member, _body())
    audit.log(
        "reservation.create",
        entity="reservation",
        entity_id=result.get("id"),
        summary=f"读者预约《{result.get('book_title') or ''}》",
    )
    return jsonify(result), 201


@bp.post("/api/reader/reservations/<int:res_id>/cancel")
def cancel_reservation(res_id: int):
    member = _require_reader()
    result = service.cancel_reservation(member, res_id)
    audit.log(
        "reservation.cancel_self",
        entity="reservation",
        entity_id=res_id,
        summary=f"读者取消预约《{result.get('book_title') or ''}》",
    )
    return jsonify(result)


# ---------------------------------------------------------------- 滞纳金


@bp.get("/api/reader/fines")
def fines():
    member = _require_reader()
    return jsonify(service.fines_overview(member))


@bp.post("/api/reader/fines/pay")
def pay_fines():
    member = _require_reader()
    result = service.pay_fines(member, _body())
    audit.log(
        "fine.pay",
        entity="fine",
        entity_id=member["id"],
        summary=f"读者自助缴纳滞纳金 ¥{float(result.get('paid_total') or 0):.2f}（{result.get('count')} 笔）",
    )
    return jsonify(result)


# ---------------------------------------------------------------- 借书证挂失


@bp.post("/api/reader/card/lost")
def report_lost():
    member = _require_reader()
    updated = service.set_card_lost(member, True)
    audit.log("card.lost", entity="member", entity_id=member["id"], summary=f"读者挂失借书证（{member.get('card_no')}）")
    return jsonify(updated)


@bp.post("/api/reader/card/unlost")
def report_unlost():
    member = _require_reader()
    updated = service.set_card_lost(member, False)
    audit.log("card.unlost", entity="member", entity_id=member["id"], summary=f"读者解除借书证挂失（{member.get('card_no')}）")
    return jsonify(updated)
