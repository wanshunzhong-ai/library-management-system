"""统一登录页与读者门户接口。

`/` 既是全站入口也是登录页：未登录时展示图书馆简介、馆藏检索与登录框；
已登录则直接送入对应的控制台，避免已登录用户反复看到登录表单。
`/api/public/*` 为只读接口，始终免登录，供检索与推荐使用。
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
