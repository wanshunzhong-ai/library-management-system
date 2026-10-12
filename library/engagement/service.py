"""读者互动业务逻辑：我的书架（收藏）、图书评价、荐购登记、资料变更申请。

四条业务的共同点是「读者产生的内容要经过一道判断才成立」：

* **收藏**：随时可加，不需要资格——它只是个人清单，不占库存也不占额度；
* **评价**：**必须借过这本书（且已归还）**才能评。否则评分可以被随手刷，
  星级也就失去了参考价值；「待评价」清单正是从已归还的借阅里推出来的；
* **荐购**：面向馆里**还没有的书**，是采购线索；馆员处理时给结论与答复；
* **资料变更**：姓名 / 读者类型这两项读者不能直接改（涉及证件与借阅权限），
  改为「申请 → 审核」。联系电话不同，它走自助修改。
  同一读者同一字段只允许一条待审申请（唯一索引兜底），避免重复提交堆成垃圾。

审核结果一律回写一条站内通知给读者，否则申请会像扔进黑洞。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Mapping

from library import db
from library.books import repository as book_repo
from library.errors import (
    BusinessRuleError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from library.engagement import repository as repo
from library.members import repository as member_repo
from library.validation import optional_str, pagination, require_choice, require_int, require_str

logger = logging.getLogger(__name__)

# 未解决的状态集合（用于「有没有待处理」的判断）
OPEN_STATUSES = ("pending",)
BOOK_REQUEST_STATUS_LABELS = {
    "pending": "待处理",
    "accepted": "已采纳",
    "purchased": "已采购",
    "rejected": "未采纳",
}
PROFILE_FIELD_LABELS = {"name": "姓名", "member_type": "读者类型"}
# 可申请变更的字段由标签表推出，避免两处各写一份、改了一处漏一处
PROFILE_FIELDS = tuple(PROFILE_FIELD_LABELS)
MEMBER_TYPES = ("学生", "教师", "社会", "VIP")
PROFILE_REQUEST_STATUS_LABELS = {"pending": "待审核", "approved": "已通过", "rejected": "已驳回"}


# ================================================================ 我的书架（收藏）


def _favorite_view(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    # member_id 只用于归属校验（仓储层读它做越权判断），不往外暴露
    item.pop("member_id", None)
    total = int(item.get("total_copies") or 0)
    available = int(item.get("available_copies") or 0)
    item["shelved"] = total > 0
    item["available_copies"] = available
    if total <= 0:
        item["state_label"] = "已下架"
    elif available > 0:
        item["state_label"] = f"可借 {available} 册"
    else:
        item["state_label"] = "已借完"
    item["rating_avg"] = round(float(item["rating_avg"]), 1) if item.get("rating_avg") is not None else 0.0
    item["rating_count"] = int(item.get("rating_count") or 0)
    return item


def list_my_favorites(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    member_id = int(member["id"])
    keyword = str(query.get("keyword") or "")
    total = repo.count_favorites(member_id, keyword)
    rows = repo.list_favorites(
        member_id,
        keyword=keyword,
        sort=str(query.get("sort") or ""),
        order=str(query.get("order") or ""),
        limit=size,
        offset=(page - 1) * size,
    )
    return {
        "items": [_favorite_view(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def add_favorite(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    book_id = require_int(payload, "book_id", label="图书", minimum=1)
    note = optional_str(payload, "note", label="备注", max_length=200, default="")
    member_id = int(member["id"])

    book = book_repo.get(book_id)
    if book is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")
    if int(book.get("total_copies") or 0) <= 0:
        raise BusinessRuleError(f"《{book['title']}》已下架，无需收藏")
    if repo.find_favorite(member_id, book_id) is not None:
        raise ConflictError(f"《{book['title']}》已在你的书架里")
    try:
        favorite_id = repo.insert_favorite(member_id, book_id, note)
    except sqlite3.IntegrityError as exc:  # UNIQUE(member_id, book_id) 兜底
        raise ConflictError(f"《{book['title']}》已在你的书架里") from exc
    return _favorite_view(repo.get_favorite(favorite_id) or {})


def remove_favorite(member: Mapping[str, Any], favorite_id: int) -> dict[str, Any]:
    row = repo.get_favorite(favorite_id)
    if row is None or int(row["member_id"] or 0) != int(member["id"]):
        # 不区分「不存在」与「不是你的」，避免用 id 探测他人书架
        raise NotFoundError(f"书架条目 #{favorite_id} 不存在")
    repo.delete_favorite(int(member["id"]), favorite_id)
    return {"deleted": favorite_id, "book_title": row.get("book_title") or ""}


def update_favorite(member: Mapping[str, Any], favorite_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    row = repo.get_favorite(favorite_id)
    if row is None or int(row["member_id"] or 0) != int(member["id"]):
        raise NotFoundError(f"书架条目 #{favorite_id} 不存在")
    note = optional_str(payload, "note", label="备注", max_length=200, default="")
    if not repo.update_favorite_note(int(member["id"]), favorite_id, note):
        raise NotFoundError(f"书架条目 #{favorite_id} 不存在")
    return _favorite_view(repo.get_favorite(favorite_id) or {})


# ================================================================ 图书评价


def _review_view(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["rating"] = int(item.get("rating") or 0)
    item["status_label"] = "已发布" if item.get("status") == "published" else "已隐藏"
    return item


def list_my_reviews(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    member_id = int(member["id"])
    filters = {
        "status": (query.get("status") or "all").strip(),
        "keyword": str(query.get("keyword") or ""),
        "member_id": member_id,
    }
    total = repo.count_reviews(filters)
    rows = repo.list_reviews(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_review_view(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def list_reviewable(member: Mapping[str, Any]) -> dict[str, Any]:
    """待评价：借过（已归还）但还没评的书。"""
    rows = repo.list_reviewable_books(int(member["id"]), limit=20)
    return {"items": [dict(row) for row in rows], "total": len(rows)}


def submit_review(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    book_id = require_int(payload, "book_id", label="图书", minimum=1)
    rating = require_int(payload, "rating", label="评分", minimum=1, maximum=5)
    content = optional_str(payload, "content", label="短评", max_length=500, default="")
    member_id = int(member["id"])

    book = book_repo.get(book_id)
    if book is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")
    if not repo.borrowed_book_ids(member_id, [book_id]):
        raise BusinessRuleError(
            f"只有借阅过《{book['title']}》的读者才能评价它——否则评分就成了随手刷的数字"
        )

    existing = repo.find_review(member_id, book_id)
    repo.upsert_review(book_id, member_id, rating, content)
    row = repo.find_review(member_id, book_id)
    logger.info(
        "读者发表图书评价",
        extra={"extra_fields": {"member_id": member_id, "book_id": book_id, "rating": rating}},
    )
    return {
        "review": _review_view(row or {}),
        "created": existing is None,
        "summary": repo.rating_summary(book_id),
    }


def delete_my_review(member: Mapping[str, Any], review_id: int) -> dict[str, Any]:
    row = repo.get_review(review_id)
    if row is None or int(row["member_id"]) != int(member["id"]):
        raise NotFoundError(f"评价 #{review_id} 不存在")
    repo.delete_review(int(member["id"]), review_id)
    return {"deleted": review_id, "summary": repo.rating_summary(int(row["book_id"]))}


def book_reviews(book_id: int, query: Mapping[str, Any], *, member_id: int | None = None) -> dict[str, Any]:
    """门户 / 读者端看到的某本书的评价（只含已发布）。"""
    book = book_repo.get(book_id)
    if book is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")
    page, size = pagination(query, default_size=5)
    total = repo.count_published_reviews(book_id)
    rows = repo.list_published_reviews(book_id, limit=size, offset=(page - 1) * size)

    items = []
    for row in rows:
        item = dict(row)
        # 评价列表只暴露「姓 + 星号」，不把别的读者的全名亮出来
        item["member_display"] = _mask_name(str(item.get("member_name") or ""))
        item.pop("member_card", None)
        item.pop("member_name", None)
        items.append(item)

    mine = None
    can_review = False
    if member_id is not None:
        own = repo.find_review(int(member_id), book_id)
        if own is not None:
            mine = {"id": own["id"], "rating": int(own["rating"]), "content": own["content"]}
        can_review = bool(repo.borrowed_book_ids(int(member_id), [book_id]))
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "summary": repo.rating_summary(book_id),
        "mine": mine,
        "can_review": can_review,
    }


def _mask_name(name: str) -> str:
    text = (name or "").strip()
    if not text:
        return "读者"
    if len(text) == 1:
        return f"{text}*"
    return text[0] + "*" * (len(text) - 1)


# ---------------------------------------------------------------- 馆员侧：评价


def list_reviews(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {
        "status": (query.get("status") or "all").strip(),
        "keyword": str(query.get("keyword") or ""),
        "rating": query.get("rating"),
    }
    total = repo.count_reviews(filters)
    rows = repo.list_reviews(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_review_view(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "status_counts": _review_status_counts(),
    }


def _review_status_counts() -> dict[str, int]:
    rows = db.query_all("SELECT status, COUNT(*) AS total FROM book_reviews GROUP BY status")
    counts = {str(row["status"]): int(row["total"]) for row in rows}
    counts["all"] = sum(counts.values())
    return counts


def set_review_status(review_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    status = require_choice(payload, "status", label="状态", choices={"published", "hidden"})
    row = repo.get_review(review_id)
    if row is None:
        raise NotFoundError(f"评价 #{review_id} 不存在")
    repo.set_review_status(review_id, status)
    return _review_view(repo.get_review(review_id) or {})


# ================================================================ 荐购登记


def _book_request_view(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    status = str(item.get("status") or "")
    item["status_label"] = BOOK_REQUEST_STATUS_LABELS.get(status, status)
    item["pending"] = status == "pending"
    return item


def _book_request_query(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    member_id = int(member["id"])
    filters = {"status": (query.get("status") or "all").strip(), "keyword": str(query.get("keyword") or "")}
    total = repo.count_book_requests(member_id, filters)
    rows = repo.list_book_requests(member_id, filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_book_request_view(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def list_my_book_requests(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    filters = {"status": "all"}
    pending = repo.count_book_requests(int(member["id"]), {**filters, "status": "pending"})
    payload = _book_request_query(member, query)
    payload["pending_total"] = pending
    return payload


def submit_book_request(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    title = require_str(payload, "title", label="书名", max_length=120)
    data = {
        "title": title,
        "author": optional_str(payload, "author", label="作者", max_length=60, default=""),
        "isbn": optional_str(payload, "isbn", label="ISBN", max_length=32, default=""),
        "publisher": optional_str(payload, "publisher", label="出版社", max_length=60, default=""),
        "reason": optional_str(payload, "reason", label="荐购理由", max_length=300, default=""),
    }
    request_id = repo.insert_book_request(int(member["id"]), data)
    logger.info(
        "读者提交荐购登记",
        extra={"extra_fields": {"member_id": member["id"], "request_id": request_id}},
    )
    return _book_request_view(repo.get_book_request(request_id) or {})


def list_book_requests(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {"status": (query.get("status") or "all").strip(), "keyword": str(query.get("keyword") or "")}
    total = repo.count_book_requests(None, filters)
    rows = repo.list_book_requests(None, filters, limit=size, offset=(page - 1) * size)
    counts = repo.book_request_counts()
    return {
        "items": [_book_request_view(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "status_counts": counts,
        "status_labels": BOOK_REQUEST_STATUS_LABELS,
    }


def review_book_request(request_id: int, payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    row = repo.get_book_request(request_id)
    if row is None:
        raise NotFoundError(f"荐购登记 #{request_id} 不存在")
    if str(row["status"]) != "pending":
        raise ConflictError("该荐购登记已处理，请勿重复操作")

    status = require_choice(
        payload, "status", label="处理结果", choices={"accepted", "purchased", "rejected"}
    )
    reply = optional_str(payload, "reply", label="答复", max_length=300, default="")
    if status == "rejected" and not reply:
        raise ValidationError("未采纳时请说明原因，读者会看到这条答复", details={"field": "reply"})

    if not repo.review_book_request(request_id, status, reply, operator):
        raise ConflictError("该荐购登记刚被他人处理，请刷新后查看")

    result = _book_request_view(repo.get_book_request(request_id) or {})
    _notify(
        int(row["member_id"]),
        "book_request",
        f"荐购《{row['title']}》已有结论：{BOOK_REQUEST_STATUS_LABELS.get(status, status)}",
        body=reply or "感谢你的荐购建议，馆员会持续关注。",
        ref_type="book_request",
        ref_id=request_id,
    )
    return result


# ================================================================ 资料变更申请


def _profile_request_view(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    status = str(item.get("status") or "")
    item["field_label"] = PROFILE_FIELD_LABELS.get(str(item.get("field") or ""), str(item.get("field") or ""))
    item["status_label"] = PROFILE_REQUEST_STATUS_LABELS.get(status, status)
    item["pending"] = status == "pending"
    return item


def list_my_profile_requests(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    member_id = int(member["id"])
    filters = {"status": "all", "keyword": ""}
    total = repo.count_profile_requests(member_id, filters)
    rows = repo.list_profile_requests(member_id, filters, limit=size, offset=(page - 1) * size)
    current = member_repo.get(member_id) or {}
    return {
        "items": [_profile_request_view(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        # 供前端渲染「可申请字段」与当前值，避免前端写死选项
        "fields": [
            {"field": "name", "label": "姓名", "value": str(current.get("name") or "")},
            {"field": "member_type", "label": "读者类型", "value": str(current.get("member_type") or "")},
        ],
        "member_types": list(MEMBER_TYPES),
    }


def submit_profile_request(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    field = require_choice(payload, "field", label="变更字段", choices=set(PROFILE_FIELDS))
    member_id = int(member["id"])
    current = member_repo.get(member_id)
    if current is None:
        raise NotFoundError("读者账号不存在")

    if field == "name":
        new_value = require_str(payload, "new_value", label="新姓名", max_length=50)
    else:
        new_value = require_choice(payload, "new_value", label="新读者类型", choices=set(MEMBER_TYPES))
    reason = optional_str(payload, "reason", label="变更说明", max_length=300, default="")

    old_value = str(current.get(field) or "")
    if new_value == old_value:
        raise ValidationError("新值与当前值相同，无需申请", details={"field": "new_value"})
    if repo.find_pending_profile_request(member_id, field) is not None:
        raise ConflictError(f"你已有一条待审核的{PROFILE_FIELD_LABELS[field]}变更申请，请等待处理")

    try:
        request_id = repo.insert_profile_request(
            member_id,
            {"field": field, "old_value": old_value, "new_value": new_value, "reason": reason},
        )
    except sqlite3.IntegrityError as exc:  # 部分唯一索引兜底
        raise ConflictError(f"你已有一条待审核的{PROFILE_FIELD_LABELS[field]}变更申请，请等待处理") from exc

    logger.info(
        "读者提交资料变更申请",
        extra={"extra_fields": {"member_id": member_id, "field": field, "request_id": request_id}},
    )
    return _profile_request_view(repo.get_profile_request(request_id) or {})


def list_profile_requests(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {"status": (query.get("status") or "all").strip(), "keyword": str(query.get("keyword") or "")}
    total = repo.count_profile_requests(None, filters)
    rows = repo.list_profile_requests(None, filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_profile_request_view(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "status_counts": repo.profile_request_counts(),
        "status_labels": PROFILE_REQUEST_STATUS_LABELS,
    }


def review_profile_request(request_id: int, payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    row = repo.get_profile_request(request_id)
    if row is None:
        raise NotFoundError(f"资料变更申请 #{request_id} 不存在")
    if str(row["status"]) != "pending":
        raise ConflictError("该申请已处理，请勿重复操作")

    approve = bool(payload.get("approve"))
    reply = optional_str(payload, "reply", label="答复", max_length=300, default="")
    if not approve and not reply:
        raise ValidationError("驳回时请说明原因，读者会看到这条答复", details={"field": "reply"})

    member_id = int(row["member_id"])
    field = str(row["field"])
    new_value = str(row["new_value"])
    member = member_repo.get(member_id)
    if member is None:
        raise NotFoundError("读者账号不存在")

    if approve:
        # 回写与状态流转在同一个事务里：不能出现「申请显示已通过、资料却没改」
        with db.transaction(immediate=True) as conn:
            if field == "name":
                conn.execute(
                    "UPDATE members SET name = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                    [new_value, member_id],
                )
            else:
                conn.execute(
                    "UPDATE members SET member_type = ?, updated_at = datetime('now', 'localtime') "
                    " WHERE id = ?",
                    [new_value, member_id],
                )
            cur = conn.execute(
                "UPDATE profile_requests SET status = 'approved', reply = ?, reviewed_by = ?, "
                "       reviewed_at = datetime('now', 'localtime'), "
                "       updated_at = datetime('now', 'localtime') "
                " WHERE id = ? AND status = 'pending'",
                [reply, operator, request_id],
            )
            changed = max(0, int(cur.rowcount or 0))
            cur.close()
            if not changed:
                raise ConflictError("该申请刚被他人处理，请刷新后查看")
        _notify(
            member_id,
            "profile",
            f"{PROFILE_FIELD_LABELS.get(field, field)}变更申请已通过",
            body=f"已更新为「{new_value}」。{reply}".strip(),
            link_view="profile",
            ref_type="profile_request",
            ref_id=request_id,
        )
    else:
        if not repo.review_profile_request(request_id, "rejected", reply, operator):
            raise ConflictError("该申请刚被他人处理，请刷新后查看")
        _notify(
            member_id,
            "profile",
            f"{PROFILE_FIELD_LABELS.get(field, field)}变更申请未通过",
            body=reply,
            link_view="profile",
            ref_type="profile_request",
            ref_id=request_id,
        )

    return _profile_request_view(repo.get_profile_request(request_id) or {})


# ================================================================ 汇总与工具


def pending_summary() -> dict[str, int]:
    """后台导航角标：三类待处理共多少条。"""
    book = repo.book_request_counts()
    profile = repo.profile_request_counts()
    reviews = _review_status_counts()
    return {
        "book_requests": int(book.get("pending") or 0),
        "profile_requests": int(profile.get("pending") or 0),
        "reviews": int(reviews.get("published") or 0),
        "total": int(book.get("pending") or 0) + int(profile.get("pending") or 0),
    }


def _notify(member_id: int, kind: str, title: str, **kwargs: Any) -> None:
    try:
        from library.notifications import service as notification_service  # noqa: PLC0415

        notification_service.notify(member_id, kind, title, **kwargs)
    except Exception:  # noqa: BLE001 - 通知是增强功能
        logger.warning("互动结果通知写入失败（已忽略）", exc_info=True)
