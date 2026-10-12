"""采编验收业务逻辑：采购登记 → 验收（去重加册 / 新建书目）→ 驳回。

## 为什么验收要按 ISBN 去重

图书馆里同一本书会多次采购。如果每次验收都新建一条书目，馆藏会被切成
「《三体》× 3 条各 1 册」，读者检索时看到三条重复结果、预约与统计也会跟着散架。
所以验收时的规则是：**馆里已有同 ISBN 就加册，没有才新建书目**。

## 与「图书批量导入」的区别

* 批量导入是「把一批既有数据灌进来」，面向已编目好的清单；
* 采编验收是「到货登记单」，带供应商、单价、册数，验收后才真正进入馆藏，
  并且留下「哪批采购进来的」这条追溯线索（``book_id`` 回填）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from library import barcode as barcode_util
from library import db
from library.acquisitions import repository as repo
from library.books import repository as book_repo
from library.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from library.validation import (
    isbn_check,
    normalize_isbn,
    optional_float,
    optional_int,
    optional_str,
    pagination,
    require_int,
    require_str,
)

logger = logging.getLogger(__name__)

STATUS_LABELS = {"pending": "待验收", "accepted": "已验收", "rejected": "已驳回"}
STATUSES = set(STATUS_LABELS)

MAX_COPIES = 9999
MAX_YEAR = 2100
MAX_BARCODE_SEQ = 10_000_000


def _decorate(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    status = str(item.get("status") or "")
    item["status_label"] = STATUS_LABELS.get(status, status)
    item["isbn_check"] = isbn_check(str(item.get("isbn") or "")) if item.get("isbn") else "unknown_format"
    item["copies"] = int(item.get("copies") or 0)
    item["unit_price"] = round(float(item.get("unit_price") or 0), 2)
    item["amount"] = round(item["copies"] * item["unit_price"], 2)
    item["can_accept"] = status == "pending"
    item["can_reject"] = status == "pending"
    # 馆里是否已有同 ISBN：验收时会「加册」而不是新建书目，先提示出来
    item["existing_book_id"] = None
    if item.get("isbn"):
        existing = repo.find_book_by_isbn(str(item["isbn"]))
        if existing is not None:
            item["existing_book_id"] = int(existing["id"])
    return item


def list_acquisitions(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    status = (query.get("status") or "").strip()
    if status and status not in STATUSES:
        raise ValidationError(
            f"状态只能是 {'/'.join(sorted(STATUSES))} 之一", details={"field": "status"}
        )
    filters = {
        "status": status,
        "keyword": query.get("keyword", ""),
        "sort": query.get("sort", ""),
        "order": query.get("order", ""),
    }
    total = repo.count(filters)
    rows = repo.list_rows(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_decorate(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "summaries": repo.summaries(filters),
        "status_labels": STATUS_LABELS,
    }


def get_acquisition(acq_id: int) -> dict[str, Any]:
    row = repo.get(int(acq_id))
    if row is None:
        raise NotFoundError(f"验收单 #{acq_id} 不存在")
    return _decorate(row)


def register(payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    """登记一张到货验收单（此时还**不进馆藏**，验收通过才生效）。"""
    isbn = _normalize_isbn(optional_str(payload, "isbn", label="ISBN", max_length=32))
    title = require_str(payload, "title", label="书名", max_length=200)
    copies = require_int(payload, "copies", label="到货册数", minimum=1, maximum=MAX_COPIES)
    unit_price = optional_float(payload, "unit_price", label="单价", minimum=0.0, default=0.0)
    publish_year = optional_int(
        payload, "publish_year", label="出版年份", minimum=1000, maximum=MAX_YEAR
    )

    with db.transaction(immediate=True) as conn:
        acq_id = repo.insert(
            conn,
            {
                "isbn": isbn,
                "title": title,
                "author": optional_str(payload, "author", label="作者", max_length=100),
                "publisher": optional_str(payload, "publisher", label="出版社", max_length=100),
                "category": optional_str(payload, "category", label="分类", max_length=50) or "未分类",
                "publish_year": publish_year,
                "location": optional_str(payload, "location", label="馆藏位置", max_length=50),
                "copies": copies,
                "unit_price": round(float(unit_price), 2),
                "supplier": optional_str(payload, "supplier", label="供应商", max_length=100),
                "note": optional_str(payload, "note", label="备注", max_length=200),
                "operator": operator,
            },
        )

    logger.info("登记采编验收单", extra={"extra_fields": {"acquisition_id": acq_id, "copies": copies}})
    return get_acquisition(acq_id)


def accept(acq_id: int, payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    """验收入库：同 ISBN 加册，否则新建书目。整条链路在同一个 IMMEDIATE 事务内完成。"""
    acq_id = int(acq_id)

    with db.transaction(immediate=True) as conn:
        row = repo.get_for_update(conn, acq_id)
        if row is None:
            raise NotFoundError(f"验收单 #{acq_id} 不存在")
        if row["status"] != "pending":
            raise ConflictError(
                f"该验收单已是「{STATUS_LABELS.get(str(row['status']), row['status'])}」，无法重复验收"
            )

        isbn = str(row.get("isbn") or "")
        copies = int(row["copies"])
        mode = ""
        book_id = 0

        existing = repo.find_book_by_isbn_conn(conn, isbn)
        if existing is not None:
            book_id = int(existing["id"])
            if not book_repo.add_copies_conn(conn, book_id, copies):
                raise ConflictError("加册失败，请刷新后重试")
            mode = "add_copies"
        else:
            if not isbn:
                raise BusinessRuleError(
                    "该验收单没有 ISBN，无法自动建档；请先在验收单上补填 ISBN，或手工在图书管理里新增"
                )
            barcode = barcode_util.normalize(
                optional_str(payload, "barcode", label="馆藏条码", max_length=64)
            )
            if barcode and not barcode_util.is_valid(barcode):
                raise ValidationError("馆藏条码需为 4–32 位字母、数字或连字符", details={"field": "barcode"})
            book_id = book_repo.insert_conn(
                conn,
                {
                    "isbn": isbn,
                    "barcode": barcode,
                    "title": row["title"],
                    "author": row.get("author") or "",
                    "publisher": row.get("publisher") or "",
                    "category": row.get("category") or "未分类",
                    "publish_year": row.get("publish_year"),
                    "location": row.get("location") or "",
                    "description": "",
                    "total_copies": copies,
                    "available_copies": copies,
                },
            )
            if not barcode:
                book_repo.set_barcode_conn(conn, book_id, _allocate_barcode_conn(conn, book_id))
            mode = "created"

        if not repo.mark_accepted(conn, acq_id, book_id=book_id, reviewer=operator):
            raise ConflictError("该验收单状态已变化，请刷新后重试")

    logger.info(
        "采编验收入库",
        extra={"extra_fields": {"acquisition_id": acq_id, "book_id": book_id, "mode": mode, "copies": copies}},
    )
    result = get_acquisition(acq_id)
    result["mode"] = mode
    result["added_copies"] = copies
    return result


def reject(acq_id: int, payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    acq_id = int(acq_id)
    reason = require_str(payload, "reason", label="驳回原因", max_length=200)
    with db.transaction(immediate=True) as conn:
        row = repo.get_for_update(conn, acq_id)
        if row is None:
            raise NotFoundError(f"验收单 #{acq_id} 不存在")
        if row["status"] != "pending":
            raise ConflictError("该验收单已有结论，无法重复处理")
        if not repo.mark_rejected(conn, acq_id, reason=reason, reviewer=operator):
            raise ConflictError("该验收单状态已变化，请刷新后重试")
    logger.info("采编验收驳回", extra={"extra_fields": {"acquisition_id": acq_id}})
    return get_acquisition(acq_id)


def delete_acquisition(acq_id: int) -> dict[str, Any]:
    acq_id = int(acq_id)
    with db.transaction(immediate=True) as conn:
        row = repo.get_for_update(conn, acq_id)
        if row is None:
            raise NotFoundError(f"验收单 #{acq_id} 不存在")
        if row["status"] != "pending":
            raise ConflictError("已验收 / 已驳回的验收单不可删除（保留作采购记录）")
    repo.delete(acq_id)
    logger.info("删除采编验收单", extra={"extra_fields": {"acquisition_id": acq_id}})
    return {"deleted": acq_id, "title": str(row.get("title") or "")}


def _normalize_isbn(raw: str) -> str:
    if not raw:
        return ""
    code = normalize_isbn(raw)
    if len(code) not in (10, 13):
        raise ValidationError("ISBN 必须是 10 位或 13 位（可含连字符）", details={"field": "isbn"})
    return code


def _allocate_barcode_conn(conn, book_id: int) -> str:
    candidate = barcode_util.generate(book_id)
    if not book_repo.exists_barcode_conn(conn, candidate):
        return candidate
    for seq in range(1, MAX_BARCODE_SEQ):
        candidate = barcode_util.generate(seq)
        if not book_repo.exists_barcode_conn(conn, candidate):
            return candidate
    raise ConflictError("条码号已用尽，请手工指定馆藏条码")
