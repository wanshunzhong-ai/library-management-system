"""图书 REST 接口。Controller 只做参数搬运，不含业务逻辑。"""

from __future__ import annotations

from typing import Any, Mapping

from flask import Blueprint, jsonify, request

from library import audit
from library.auth.permissions import require_permission
from library.books import service

bp = Blueprint("books", __name__, url_prefix="/api/books")


@bp.get("")
def list_books():
    return jsonify(service.list_books(request.args.to_dict(flat=True)))


@bp.get("/categories")
def list_categories():
    return jsonify({"items": service.list_categories()})


@bp.get("/<int:book_id>")
def get_book(book_id: int):
    return jsonify(service.get_book(book_id))


def _title_of(payload: Mapping[str, Any] | None) -> str:
    return str((payload or {}).get("title") or "")


def _shelf_summary(_kwargs: Mapping[str, Any], payload: Mapping[str, Any] | None) -> str:
    data: Mapping[str, Any] = payload or {}
    title = data.get("title") or ""
    total = int(data.get("total_copies") or 0)
    verb = "下架" if total <= 0 else f"调整馆藏为 {total} 册"
    return f"{verb}《{title}》"


@bp.post("")
@require_permission("book:write")
@audit.audit_action("book.create", entity="book", summary_of=lambda _kw, p: f"新增图书《{_title_of(p)}》")
def create_book():
    book = service.create_book(request.get_json(silent=True) or {})
    return jsonify(book), 201


@bp.put("/<int:book_id>")
@require_permission("book:write")
@audit.audit_action("book.update", entity="book", summary_of=lambda _kw, p: f"修改图书《{_title_of(p)}》")
def update_book(book_id: int):
    return jsonify(service.update_book(book_id, request.get_json(silent=True) or {}))


@bp.post("/<int:book_id>/shelf")
@require_permission("book:write")
@audit.audit_action("book.shelf", entity="book", summary_of=_shelf_summary)
def set_shelf(book_id: int):
    """上架 / 下架 / 调库：`{"total_copies": n}`，0 表示下架。"""
    return jsonify(service.set_total_copies(book_id, request.get_json(silent=True) or {}))


@bp.delete("/<int:book_id>")
@require_permission("book:write")
@audit.audit_action("book.delete", entity="book", summary_of=lambda _kw, p: f"删除图书《{_title_of(p)}》")
def delete_book(book_id: int):
    # 先取书名：删掉之后就没有可读的标题给审计日志了（响应里回带 title 供前端提示与留痕）
    book = service.get_book(book_id)
    service.delete_book(book_id)
    return jsonify({"deleted": book_id, "title": book.get("title") or ""})
