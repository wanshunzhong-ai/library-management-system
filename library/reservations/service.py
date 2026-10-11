"""预约业务逻辑：读者排队预约 -> 到书通知 -> 借出完成。

设计要点：

* **只对「暂无可借复本」开放预约**。有可借复本时让读者直接到馆借阅，
  否则预约队列会充斥"本来就能马上借到"的无效单，到书通知也失去意义。
* **到书自动唤队**：还书回补库存后，把该书最早的一条 ``waiting`` 预约置为 ``ready``
  —— 这就是"到书通知"的落点（读者下次进入个人中心即可看到"已到书，请到馆借阅"）。
* **借出自动结单**：读者借到同一本书时，把其未结束预约置为 ``fulfilled``。
* 这两个联动都写在 ``library.loans.service`` 的借还路径里，且**失败只告警不抛出**
  —— 预约是增强功能，绝不能因为预约表出问题就让一次正常的借书 / 还书失败。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Mapping

from library import db
from library.books import repository as book_repo
from library.errors import BusinessRuleError, ConflictError, ForbiddenError, NotFoundError, ValidationError
from library.loans import repository as loan_repo
from library.members import repository as member_repo
from library.reservations import repository as repo
from library.validation import optional_str, pagination

logger = logging.getLogger(__name__)

STATUS_LABELS = {
    "waiting": "排队中",
    "ready": "已到书",
    "fulfilled": "已借出",
    "cancelled": "已取消",
    "expired": "已过期",
}
MEMBER_STATUS_FILTERS = {"all", "open", "waiting", "ready", "fulfilled", "cancelled", "expired"}


def _decorate(row: Mapping[str, Any], *, member_id: int | None = None) -> dict[str, Any]:
    item = dict(row)
    status = str(item.get("status") or "")
    item["status_label"] = STATUS_LABELS.get(status, status)
    item["book_available"] = int(item.get("available_copies") or 0) > 0
    # 排队位次：仅排队中有意义（排在第几位，1 表示最前）
    if status == "waiting":
        item["queue_position"] = int(repo.waiting_ahead(int(item["book_id"]), int(item["id"]))) + 1
    else:
        item["queue_position"] = None
    # 读者端：能否自行取消（未结束即可取消）
    item["can_cancel"] = status in repo.OPEN_STATUSES and (
        member_id is None or int(item.get("member_id") or 0) == int(member_id)
    )
    item["can_ready"] = status == "waiting"
    return item


def get_reservation(res_id: int, *, member_id: int | None = None) -> dict[str, Any]:
    row = repo.get(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    return _decorate(row, member_id=member_id)


# ---------------------------------------------------------------- 读者侧


def reserve(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """提交预约。校验与写入放在同一个 IMMEDIATE 事务里，避免并发下重复排队。

    INSERT 直接走事务内的 ``conn.execute``（而不是 ``db.execute``），后者自带事务，
    会在中途提交外层事务、破坏"读-判断-写"的原子性。
    """
    book_id = _require_int(payload, "book_id", label="图书")
    note = optional_str(payload, "note", label="备注", max_length=200)
    member_id = int(member["id"])

    with db.transaction(immediate=True) as conn:
        current = member_repo.get(member_id)
        if current is None:
            raise NotFoundError("读者账号不存在")
        if str(current.get("status") or "active") != "active":
            raise BusinessRuleError("账号已被冻结，暂时无法预约，请联系馆员")
        if int(current.get("card_lost") or 0):
            raise BusinessRuleError("借书证已挂失，请先解除挂失再预约")

        book = book_repo.get(book_id)
        if book is None:
            raise NotFoundError(f"图书 #{book_id} 不存在")
        if int(book.get("total_copies") or 0) <= 0:
            raise BusinessRuleError(f"《{book['title']}》已下架，无法预约")
        if loan_repo.find_open_loan(book_id, member_id) is not None:
            raise ConflictError(f"你已借阅《{book['title']}》，请先归还再预约")

        available = int(book.get("available_copies") or 0)
        if available > 0:
            raise BusinessRuleError(
                f"《{book['title']}》当前有 {available} 册可借，可直接到馆借阅，无需预约"
            )
        if repo.find_open(book_id, member_id) is not None:
            raise ConflictError(f"你已预约《{book['title']}》，请勿重复提交")

        try:
            cur = conn.execute(
                "INSERT INTO reservations (book_id, member_id, status, note) "
                "VALUES (?, ?, 'waiting', ?)",
                [book_id, member_id, note],
            )
            res_id = int(cur.lastrowid or 0)
            cur.close()
        except sqlite3.IntegrityError as exc:
            # 唯一索引 idx_reservations_open_unique 兜底：并发下重复预约
            raise ConflictError(f"你已预约《{book['title']}》，请勿重复提交") from exc

    logger.info(
        "读者提交预约",
        extra={"extra_fields": {"member_id": member_id, "book_id": book_id, "reservation_id": res_id}},
    )
    return get_reservation(res_id, member_id=member_id)


def cancel_my_reservation(member: Mapping[str, Any], res_id: int) -> dict[str, Any]:
    row = repo.raw(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    if int(row["member_id"]) != int(member["id"]):
        logger.warning(
            "读者尝试取消他人预约",
            extra={"extra_fields": {"member_id": member["id"], "reservation_id": res_id}},
        )
        raise ForbiddenError("只能取消本人的预约")
    if str(row["status"]) not in repo.OPEN_STATUSES:
        raise ConflictError("该预约已结束，无法取消")

    repo.close(res_id, "cancelled")
    logger.info("读者取消预约", extra={"extra_fields": {"member_id": member["id"], "reservation_id": res_id}})
    return get_reservation(res_id, member_id=member["id"])


def list_my_reservations(member: Mapping[str, Any], query: Mapping[str, Any]) -> dict[str, Any]:
    status = (query.get("status") or "all").strip()
    if status not in MEMBER_STATUS_FILTERS:
        raise ValidationError(
            f"status 只能是 {'/'.join(sorted(MEMBER_STATUS_FILTERS))}", details={"field": "status"}
        )
    page, size = pagination(query)
    member_id = int(member["id"])
    total = repo.count_for_member(member_id, status)
    rows = repo.list_for_member(member_id, status, limit=size, offset=(page - 1) * size)
    items = [_decorate(row, member_id=member_id) for row in rows]
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "open_total": repo.count_for_member(member_id, "open"),
    }


# ---------------------------------------------------------------- 馆员侧


def list_reservations(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    filters = {
        "status": (query.get("status") or "").strip(),
        "keyword": query.get("keyword", ""),
    }
    total = repo.count_all(filters)
    rows = repo.list_all(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_decorate(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "waiting_total": repo.count_all({"status": "waiting"}),
    }


def mark_ready(res_id: int) -> dict[str, Any]:
    """馆员手动通知到书（自动唤队之外的补充手段）。"""
    row = repo.raw(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    if str(row["status"]) != "waiting":
        raise BusinessRuleError("只有排队中的预约才能通知到书")
    repo.mark_ready(res_id)
    return get_reservation(res_id)


def cancel_reservation(res_id: int) -> dict[str, Any]:
    row = repo.raw(res_id)
    if row is None:
        raise NotFoundError(f"预约 #{res_id} 不存在")
    if str(row["status"]) not in repo.OPEN_STATUSES:
        raise ConflictError("该预约已结束，无法取消")
    repo.close(res_id, "cancelled")
    return get_reservation(res_id)


# ---------------------------------------------------------------- 借还联动（供 loans.service 调用）


def on_book_returned(book_id: int) -> None:
    """还书回补库存后唤队：有可借复本就把最早的若干条排队预约置为「已到书」。

    最多通知与当前可借复本数相等的条数（不虚报"到书"），且不占用库存——
    到书读者仍需自己到馆办理借出。整个流程包在 try/except 里：预约是增强功能，
    任何异常都只告警，绝不能影响还书本身是否成功。
    """
    try:
        book = book_repo.get(book_id)
        if book is None:
            return
        remaining = int(book.get("available_copies") or 0)
        while remaining > 0:
            nxt = repo.next_waiting(book_id)
            if nxt is None or not repo.mark_ready(int(nxt["id"])):
                break
            logger.info(
                "预约到书已通知",
                extra={"extra_fields": {"reservation_id": nxt["id"], "book_id": book_id}},
            )
            remaining -= 1
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("预约唤队失败（已忽略）", exc_info=True)


def on_book_borrowed(book_id: int, member_id: int) -> None:
    """借出成功后结单：把该读者对该书的未结束预约置为「已借出」。"""
    try:
        row = repo.find_open(book_id, member_id)
        if row is not None and repo.mark_fulfilled(int(row["id"])):
            logger.info(
                "预约已转为已借出",
                extra={"extra_fields": {"reservation_id": row["id"], "book_id": book_id, "member_id": member_id}},
            )
    except Exception:  # noqa: BLE001 - 见 docstring
        logger.warning("预约结单失败（已忽略）", exc_info=True)


def _require_int(payload: Mapping[str, Any], key: str, *, label: str) -> int:
    value = payload.get(key)
    if value in (None, ""):
        raise ValidationError(f"{label}不能为空", details={"field": key})
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} ID 必须是整数", details={"field": key}) from exc
    if number <= 0:
        raise ValidationError(f"{label} ID 非法", details={"field": key})
    return number
