"""图书业务逻辑：不依赖任何 HTTP 对象。"""

from __future__ import annotations

from typing import Any, Mapping

from library import db
from library.books import repository as repo
from library.errors import ConflictError, NotFoundError, ValidationError
from library.validation import (
    normalize_isbn,
    optional_int,
    optional_str,
    pagination,
    require_int,
    require_str,
)

MAX_YEAR = 2100


def _normalize(payload: Mapping[str, Any]) -> dict[str, Any]:
    isbn_raw = require_str(payload, "isbn", label="ISBN", max_length=32)
    isbn = normalize_isbn(isbn_raw)
    if len(isbn) not in (10, 13):
        raise ValidationError("ISBN 必须是 10 位或 13 位（可含连字符）", details={"field": "isbn"})

    total_copies = require_int(payload, "total_copies", label="总册数", minimum=1, maximum=9999)
    publish_year = optional_int(payload, "publish_year", label="出版年份", minimum=1000, maximum=MAX_YEAR)

    return {
        "isbn": isbn,
        "title": require_str(payload, "title", label="书名", max_length=200),
        "author": optional_str(payload, "author", label="作者", max_length=100),
        "publisher": optional_str(payload, "publisher", label="出版社", max_length=100),
        "category": optional_str(payload, "category", label="分类", max_length=50) or "未分类",
        "publish_year": publish_year,
        "location": optional_str(payload, "location", label="馆藏位置", max_length=50),
        "total_copies": total_copies,
    }


def list_books(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {
        "keyword": query.get("keyword", ""),
        "category": query.get("category", ""),
        "available_only": str(query.get("available_only", "")).lower() in {"1", "true", "yes"},
    }
    total = repo.count(filters)
    items = repo.list_books(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def get_book(book_id: int) -> dict[str, Any]:
    book = repo.get(book_id)
    if book is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")
    book["active_loans"] = repo.active_loan_count(book_id)
    return book


def create_book(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = _normalize(payload)
    if repo.exists_isbn(data["isbn"]):
        raise ConflictError(f"ISBN {data['isbn']} 已存在", details={"field": "isbn"})
    data["available_copies"] = data["total_copies"]
    book_id = repo.insert(data)
    return get_book(book_id)


def update_book(book_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    current = repo.get(book_id)
    if current is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")

    data = _normalize(payload)
    if repo.exists_isbn(data["isbn"], exclude_id=book_id):
        raise ConflictError(f"ISBN {data['isbn']} 已被其它图书占用", details={"field": "isbn"})

    active = repo.active_loan_count(book_id)
    if data["total_copies"] < active:
        raise ConflictError(f"当前有 {active} 册在外借阅，总册数不能小于 {active}")

    # 可借数 = 新总册数 - 在借数
    data["available_copies"] = data["total_copies"] - active
    repo.update(book_id, data)
    return get_book(book_id)


def delete_book(book_id: int) -> None:
    if repo.get(book_id) is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")
    active = repo.active_loan_count(book_id)
    if active:
        raise ConflictError(f"该书还有 {active} 册未归还，无法删除")
    if _has_loan_history(book_id):
        raise ConflictError("该书存在历史借阅记录，为保留档案不可删除；可将总册数改为 0 下架")
    repo.delete(book_id)


def _has_loan_history(book_id: int) -> bool:
    return bool(db.query_scalar("SELECT 1 FROM loans WHERE book_id = ? LIMIT 1", [book_id]))


def list_categories() -> list[dict[str, Any]]:
    return repo.categories()
