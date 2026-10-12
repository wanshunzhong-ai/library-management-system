"""图书业务逻辑：不依赖任何 HTTP 对象。"""

from __future__ import annotations

from typing import Any, Mapping

from library import barcode as barcode_util
from library import db
from library.books import repository as repo
from library.errors import ConflictError, NotFoundError, ValidationError
from library.validation import (
    isbn_check,
    normalize_isbn,
    optional_int,
    optional_str,
    pagination,
    require_int,
    require_str,
)

MAX_YEAR = 2100
MAX_DESCRIPTION = 1000
MAX_BARCODE_SEQ = 10_000_000


def _decorate(book: Mapping[str, Any]) -> dict[str, Any]:
    """补派生标记：总册数为 0 即「已下架」；ISBN 校验位结果供前端提示。"""
    item = dict(book)
    item["off_shelf"] = int(item.get("total_copies") or 0) <= 0
    item["isbn_check"] = isbn_check(str(item.get("isbn") or ""))
    return item


def _normalize(payload: Mapping[str, Any]) -> dict[str, Any]:
    isbn_raw = require_str(payload, "isbn", label="ISBN", max_length=32)
    isbn = normalize_isbn(isbn_raw)
    if len(isbn) not in (10, 13):
        raise ValidationError("ISBN 必须是 10 位或 13 位（可含连字符）", details={"field": "isbn"})

    total_copies = require_int(payload, "total_copies", label="总册数", minimum=0, maximum=9999)
    publish_year = optional_int(payload, "publish_year", label="出版年份", minimum=1000, maximum=MAX_YEAR)

    barcode = barcode_util.normalize(optional_str(payload, "barcode", label="馆藏条码", max_length=64))
    if barcode and not barcode_util.is_valid(barcode):
        raise ValidationError(
            "馆藏条码需为 4–32 位字母、数字或连字符", details={"field": "barcode"}
        )

    return {
        "isbn": isbn,
        "barcode": barcode,
        "title": require_str(payload, "title", label="书名", max_length=200),
        "author": optional_str(payload, "author", label="作者", max_length=100),
        "publisher": optional_str(payload, "publisher", label="出版社", max_length=100),
        "category": optional_str(payload, "category", label="分类", max_length=50) or "未分类",
        "publish_year": publish_year,
        "location": optional_str(payload, "location", label="馆藏位置", max_length=50),
        "description": optional_str(
            payload, "description", label="图书简介", max_length=MAX_DESCRIPTION
        ),
        "total_copies": total_copies,
    }


def _allocate_barcode(book_id: int) -> str:
    """为新书分配条码号：优先用图书 id，被占用则顺延到第一个空位。"""
    candidate = barcode_util.generate(book_id)
    if not repo.exists_barcode(candidate, exclude_id=book_id):
        return candidate
    for seq in range(1, MAX_BARCODE_SEQ):
        candidate = barcode_util.generate(seq)
        if not repo.exists_barcode(candidate, exclude_id=book_id):
            return candidate
    raise ConflictError("条码号已用尽，请手工指定馆藏条码")


def validate_book(payload: Mapping[str, Any]) -> dict[str, Any]:
    """只做规范化与唯一性校验，**不写库**（批量导入预览用）。

    与 ``create_book`` 共用同一套 ``_normalize`` 与冲突判定，避免「预览说没问题、
    真导入却报错」这种两套口径的经典坑。
    """
    data = _normalize(payload)
    if repo.exists_isbn(data["isbn"]):
        raise ConflictError(f"ISBN {data['isbn']} 已存在", details={"field": "isbn"})
    if data["barcode"] and repo.exists_barcode(data["barcode"]):
        raise ConflictError(
            f"馆藏条码 {data['barcode']} 已被其它图书占用", details={"field": "barcode"}
        )
    data["isbn_check"] = isbn_check(data["isbn"])
    return data


def list_books(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {
        "keyword": query.get("keyword", ""),
        "category": query.get("category", ""),
        "available_only": str(query.get("available_only", "")).lower() in {"1", "true", "yes"},
        # 排序：未命中白名单时由仓库层回落到默认顺序，非法值不报错
        "sort": query.get("sort", ""),
        "order": query.get("order", ""),
    }
    total = repo.count(filters)
    items = [_decorate(b) for b in repo.list_books(filters, limit=size, offset=(page - 1) * size)]
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
    book = _decorate(book)
    book["active_loans"] = repo.active_loan_count(book_id)
    return book


def create_book(payload: Mapping[str, Any]) -> dict[str, Any]:
    data = _normalize(payload)
    if repo.exists_isbn(data["isbn"]):
        raise ConflictError(f"ISBN {data['isbn']} 已存在", details={"field": "isbn"})
    if data["barcode"] and repo.exists_barcode(data["barcode"]):
        raise ConflictError(
            f"馆藏条码 {data['barcode']} 已被其它图书占用", details={"field": "barcode"}
        )

    data["available_copies"] = data["total_copies"]
    book_id = repo.insert(data)
    if not data["barcode"]:
        # 条码号需要图书 id 才能生成，插入后回写
        repo.set_barcode(book_id, _allocate_barcode(book_id))
    return get_book(book_id)


def update_book(book_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    current = repo.get(book_id)
    if current is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")

    data = _normalize(payload)
    if repo.exists_isbn(data["isbn"], exclude_id=book_id):
        raise ConflictError(f"ISBN {data['isbn']} 已被其它图书占用", details={"field": "isbn"})

    # 条码留空表示沿用原条码，不会把已有条码清空
    barcode = data["barcode"] or str(current.get("barcode") or "")
    if not barcode:
        barcode = _allocate_barcode(book_id)
    if repo.exists_barcode(barcode, exclude_id=book_id):
        raise ConflictError(f"馆藏条码 {barcode} 已被其它图书占用", details={"field": "barcode"})
    data["barcode"] = barcode

    active = repo.active_loan_count(book_id)
    if data["total_copies"] < active:
        raise ConflictError(
            f"《{current['title']}》当前有 {active} 册在外借阅，总册数不能小于 {active}"
            + ("（要下架请先办理还书）" if data["total_copies"] == 0 else ""),
            details={"field": "total_copies"},
        )

    # 可借数 = 新总册数 - 在借数（总册数为 0 即下架）
    data["available_copies"] = data["total_copies"] - active
    repo.update(book_id, data)
    return get_book(book_id)


def delete_book(book_id: int) -> None:
    book = repo.get(book_id)
    if book is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")
    active = repo.active_loan_count(book_id)
    if active:
        raise ConflictError(f"《{book['title']}》还有 {active} 册未归还，无法删除")
    if _has_loan_history(book_id):
        raise ConflictError(
            f"《{book['title']}》存在历史借阅记录，为保留档案不可删除；"
            "请改用「下架」（总册数改为 0），下架后不再对读者展示"
        )
    repo.delete(book_id)


def set_total_copies(book_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """调整总册数：填 0 即「下架」，填正数即「上架 / 调库」。

    下架要求该书当前没有在外借出的册数（否则先把册数还回来）；任何情况下都保证
    ``available_copies = total_copies - 在借数``。校验与写入同处一个 IMMEDIATE 事务，
    避免与借书 / 还书并发时算错库存。
    """
    total = require_int(payload, "total_copies", label="总册数", minimum=0, maximum=9999)

    with db.transaction(immediate=True) as conn:
        current = repo.get(book_id)
        if current is None:
            raise NotFoundError(f"图书 #{book_id} 不存在")

        active = repo.active_loan_count(book_id)
        if total < active:
            if total == 0:
                raise ConflictError(
                    f"《{current['title']}》还有 {active} 册未归还，无法下架；请先办理还书",
                    details={"field": "total_copies"},
                )
            raise ConflictError(
                f"《{current['title']}》当前有 {active} 册在外借阅，总册数不能小于 {active}",
                details={"field": "total_copies"},
            )

        repo.update_copies(book_id, total, total - active)

    return get_book(book_id)


def _has_loan_history(book_id: int) -> bool:
    return bool(db.query_scalar("SELECT 1 FROM loans WHERE book_id = ? LIMIT 1", [book_id]))


def list_categories() -> list[dict[str, Any]]:
    return repo.categories()
