"""读者互动的馆员侧接口：评价管理、荐购处理、资料变更审核。

读者自己的接口（收藏 / 写评价 / 荐购登记 / 提交变更申请）在 ``library.reader.routes``，
因为它们必须落在 ``/api/reader/`` 前缀下才能拿到读者会话与读者 CSRF。
这里只放**馆员**动作，统一挂在 ``/api/engagement`` 下、按 ``member:write`` 授权。
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from library import audit
from library.auth import service as auth_service
from library.auth.permissions import require_permission
from library.engagement import service

bp = Blueprint("engagement", __name__, url_prefix="/api/engagement")


def _operator() -> str:
    return str((auth_service.current_admin() or {}).get("username") or "")


@bp.get("/summary")
@require_permission("member:read")
def summary():
    """三类待处理的数量（后台导航角标用）。"""
    return jsonify(service.pending_summary())


# ---------------------------------------------------------------- 图书评价


@bp.get("/reviews")
@require_permission("member:read")
def list_reviews():
    return jsonify(service.list_reviews(request.args.to_dict(flat=True)))


@bp.post("/reviews/<int:review_id>/status")
@require_permission("member:write")
@audit.audit_action(
    "review.status",
    entity="review",
    summary_of=lambda kw, p: f"把图书评价 #{kw.get('review_id')} 置为「{str((p or {}).get('status') or '')}」",
)
def set_review_status(review_id: int):
    return jsonify(service.set_review_status(review_id, request.get_json(silent=True) or {}))


# ---------------------------------------------------------------- 荐购登记


@bp.get("/book-requests")
@require_permission("member:read")
def list_book_requests():
    return jsonify(service.list_book_requests(request.args.to_dict(flat=True)))


@bp.post("/book-requests/<int:request_id>/review")
@require_permission("member:write")
@audit.audit_action(
    "book_request.review",
    entity="book_request",
    summary_of=lambda kw, p: f"处理荐购登记 #{kw.get('request_id')}：{str((p or {}).get('status') or '')}",
)
def review_book_request(request_id: int):
    return jsonify(
        service.review_book_request(request_id, request.get_json(silent=True) or {}, operator=_operator())
    )


# ---------------------------------------------------------------- 资料变更申请


@bp.get("/profile-requests")
@require_permission("member:read")
def list_profile_requests():
    return jsonify(service.list_profile_requests(request.args.to_dict(flat=True)))


@bp.post("/profile-requests/<int:request_id>/review")
@require_permission("member:write")
@audit.audit_action(
    "profile_request.review",
    entity="profile_request",
    summary_of=lambda kw, p: (
        f"{'通过' if (p or {}).get('approve') else '驳回'}资料变更申请 #{kw.get('request_id')}"
    ),
)
def review_profile_request(request_id: int):
    return jsonify(
        service.review_profile_request(
            request_id, request.get_json(silent=True) or {}, operator=_operator()
        )
    )
