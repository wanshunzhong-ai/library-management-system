"""概览接口。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library.auth.permissions import require_permission
from library.dashboard import service

bp = Blueprint("dashboard", __name__, url_prefix="/api/dashboard")


@bp.get("/overview")
@require_permission("dashboard:read")
def overview():
    return jsonify(service.overview(request.args.to_dict(flat=True)))
