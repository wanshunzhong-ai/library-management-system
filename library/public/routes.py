"""统一登录页与读者门户接口。

`/` 既是全站入口也是登录页：未登录时展示图书馆简介、公告、馆藏检索与登录框；
已登录则直接送入对应的控制台，避免已登录用户反复看到登录表单。
`/api/public/*` 为免登录接口：只读检索与公告之外，还开放两个**受控写入口**
（办证申请、借阅速查）——两者都不建会话、都按来源限流，详见 service 的说明。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, redirect, render_template, request, url_for

from config import get_config
from library.auth import service as auth_service
from library.public import service
from library.reader import service as reader_service

bp = Blueprint("public", __name__)


@bp.get("/")
def home():
    if auth_service.current_admin() is not None:
        return redirect(url_for("admin_console"))
    if reader_service.current_reader() is not None:
        return redirect(url_for("reader.home"))
    cfg = get_config()
    return render_template(
        "public.html",
        library_name=cfg.library_name,
        library_slogan=cfg.library_slogan,
    )


@bp.get("/api/public/overview")
def overview():
    return jsonify(service.overview())


@bp.get("/api/public/search")
def search():
    return jsonify(service.search(request.args.to_dict(flat=True)))


@bp.get("/api/public/books/<int:book_id>/reviews")
def book_reviews(book_id: int):
    """门户详情弹窗里的读者评价：免登录只读，只返回已发布评价。"""
    return jsonify(service.book_reviews(book_id, request.args.to_dict(flat=True)))


@bp.post("/api/public/quick-check")
def quick_check():
    """免登录借阅速查：只读、脱敏、共用登录限流桶。"""
    return jsonify(service.quick_check(request.get_json(silent=True) or {}, ip=auth_service.client_ip()))


@bp.post("/api/public/member-applications")
def submit_application():
    """在线办证预登记：提交后由馆员审核发证。"""
    payload = service.submit_application(request.get_json(silent=True) or {}, ip=auth_service.client_ip())
    return jsonify(payload), 201


@bp.post("/api/public/member-applications/lookup")
def lookup_application():
    """凭「申请编号 + 手机号」查询办证进度。"""
    return jsonify(
        service.lookup_application(request.get_json(silent=True) or {}, ip=auth_service.client_ip())
    )
