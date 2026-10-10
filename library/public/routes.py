"""读者门户接口（免登录，只读）。"""

from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request

from config import get_config
from library.public import service

bp = Blueprint("public", __name__)


@bp.get("/")
def home():
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
