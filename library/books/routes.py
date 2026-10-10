"""图书 REST 接口。Controller 只做参数搬运，不含业务逻辑。"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

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


@bp.post("")
def create_book():
    book = service.create_book(request.get_json(silent=True) or {})
    return jsonify(book), 201


@bp.put("/<int:book_id>")
def update_book(book_id: int):
    return jsonify(service.update_book(book_id, request.get_json(silent=True) or {}))


@bp.delete("/<int:book_id>")
def delete_book(book_id: int):
    service.delete_book(book_id)
    return jsonify({"deleted": book_id})
