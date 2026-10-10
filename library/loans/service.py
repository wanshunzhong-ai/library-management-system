"""借阅业务逻辑：借书 / 还书 / 续借，含库存联动与并发保护。

并发要点：借书 / 还书 / 续借都是「读-判断-写」，校验必须在 **同一个 IMMEDIATE
事务**内完成（见 ``db.transaction(immediate=True)``），否则事务外的快照会让并发
请求双双通过校验 —— 表现为超可借上限、同一本书重复在借、续借次数超限、库存重复回补。
数据库层另有唯一索引与 CHECK 约束兜底。
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Mapping

from config import get_config
from library import db, fine, timeutil
from library.books import repository as book_repo
from library.errors import AppError, BusinessRuleError, ConflictError, NotFoundError, ValidationError
from library.loans import repository as repo
from library.members import repository as member_repo
from library.validation import optional_str, pagination

logger = logging.getLogger(__name__)

STATUS_LABELS = {"borrowed": "在借", "overdue": "逾期", "returned": "已归还"}

# 单次批量还书上限：柜面一次性归还的合理规模，也避免一次请求长时间占用写锁
MAX_BATCH_RETURN = 200


def _decorate(row: Mapping[str, Any]) -> dict[str, Any]:
    """补齐派生字段：状态、逾期天数、罚金。

    状态与天数走 ``library.fine`` 的同一口径（整日），因此
    「状态 = 逾期」与「逾期天数 ≥ 1」永远等价；罚金同样由 ``fine`` 计算并封顶。
    """
    item = dict(row)
    settlement = fine.loan_settlement(item.get("due_at"), item.get("returned_at"))

    if item.get("returned_at"):
        item["status"] = "returned"
    elif settlement["days"] > 0:
        item["status"] = "overdue"
    else:
        item["status"] = "borrowed"

    item["overdue_days"] = settlement["days"]
    item["fine"] = settlement["fine"]
    item["fine_capped"] = settlement["capped"]
    item["status_label"] = STATUS_LABELS[item["status"]]
    # 可续借 = 在借 + 未逾期 + 未达次数上限 + 读者未被冻结。
    # 与 renew() 的真实闸门保持同一口径，避免出现「按钮可点但必然失败」。
    item["can_renew"] = (
        item["status"] == "borrowed"
        and int(item.get("renew_count") or 0) < get_config().max_renew
        and (item.get("member_status") or "active") == "active"
    )
    return item


def serialize_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """供其它模块（如概览）复用的序列化入口。"""
    return [_decorate(row) for row in rows]


def list_loans(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    status = (query.get("status") or "all").strip()
    if status not in {"all", "borrowed", "overdue", "returned", "open"}:
        raise ValidationError("status 只能是 all/borrowed/overdue/returned/open", details={"field": "status"})

    filters = {
        "status": "" if status == "all" else status,
        "keyword": query.get("keyword", ""),
        "member_id": query.get("member_id") or 0,
        "book_id": query.get("book_id") or 0,
        # 排序：未命中白名单时由仓库层回落到默认顺序（未归还优先 + 应还日期升序）
        "sort": query.get("sort", ""),
        "order": query.get("order", ""),
    }
    total = repo.count(filters)
    rows = repo.list_loans(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_decorate(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def get_loan(loan_id: int) -> dict[str, Any]:
    row = repo.get(loan_id)
    if row is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    return _decorate(row)


def borrow(payload: Mapping[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    book_id = _require_id(payload, "book_id", label="图书")
    member_id = _require_id(payload, "member_id", label="读者")
    note = optional_str(payload, "note", label="备注", max_length=200)

    started = timeutil.now()
    due = timeutil.plus_days(started, cfg.loan_days)

    # 校验与写入必须在同一个 IMMEDIATE 事务内：事务开始即取写锁，
    # 并发借书被串行化，第二个请求读到的是前一个提交后的最新数据。
    # 事务内的 repo 查询走同一线程的同一连接（db.get_conn），因此天然在同一事务里。
    with db.transaction(immediate=True) as conn:
        member = member_repo.get(member_id)
        if member is None:
            raise NotFoundError(f"读者 #{member_id} 不存在")
        if member["status"] != "active":
            raise BusinessRuleError(f"读者「{member['name']}」已被冻结，无法借书")

        book = book_repo.get(book_id)
        if book is None:
            raise NotFoundError(f"图书 #{book_id} 不存在")
        if int(book["total_copies"]) <= 0:
            raise BusinessRuleError(f"《{book['title']}》已下架，无法借出")

        if member_repo.overdue_count(member_id) > 0:
            raise BusinessRuleError(f"读者「{member['name']}」有逾期未还图书，请先归还")
        if member_repo.active_loan_count(member_id) >= int(member["max_borrow"]):
            raise BusinessRuleError(f"读者「{member['name']}」已达可借上限 {member['max_borrow']} 册")
        if repo.find_open_loan(book_id, member_id) is not None:
            raise ConflictError(f"读者已借阅《{book['title']}》且尚未归还")

        # 库存扣减用条件 UPDATE，避免并发下超借
        cur = conn.execute(
            "UPDATE books SET available_copies = available_copies - 1, "
            "       updated_at = datetime('now', 'localtime') "
            " WHERE id = ? AND available_copies > 0",
            [book_id],
        )
        if cur.rowcount == 0:
            raise BusinessRuleError(f"《{book['title']}》当前无可借库存")

        try:
            cur = conn.execute(
                "INSERT INTO loans (book_id, member_id, borrowed_at, due_at, renew_count, note) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                [book_id, member_id, timeutil.to_str(started), timeutil.to_str(due), note],
            )
        except sqlite3.IntegrityError as exc:
            # 唯一索引 idx_loans_open_unique 兜底：极端并发下仍撞上「同一读者同一本书未归还」
            raise ConflictError(f"读者已借阅《{book['title']}》且尚未归还") from exc
        loan_id = int(cur.lastrowid or 0)

    return get_loan(loan_id)


def return_book(loan_id: int) -> dict[str, Any]:
    loan = repo.raw(loan_id)
    if loan is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    if loan["returned_at"]:
        raise ConflictError("该借阅记录已归还，无需重复操作")

    with db.transaction(immediate=True) as conn:
        # 只有由本次请求真正把记录置为「已归还」时才回补库存：
        # 并发重复还书的行数为 0，不能给它回补库存（否则可借数会凭空增加）。
        cur = conn.execute(
            "UPDATE loans SET returned_at = ? WHERE id = ? AND returned_at IS NULL",
            [timeutil.to_str(timeutil.now()), loan_id],
        )
        if cur.rowcount == 0:
            raise ConflictError("该借阅记录已归还，无需重复操作")

        # 回补库存加上界：可借数不得超过总册数
        restored = conn.execute(
            "UPDATE books SET available_copies = available_copies + 1, "
            "       updated_at = datetime('now', 'localtime') "
            " WHERE id = ? AND available_copies < total_copies",
            [loan["book_id"]],
        )
        if restored.rowcount == 0:
            logger.warning(
                "还书未回补库存（可借数已达总册数）",
                extra={"extra_fields": {"loan_id": loan_id, "book_id": loan["book_id"]}},
            )

    return get_loan(loan_id)


def return_batch(payload: Mapping[str, Any]) -> dict[str, Any]:
    """批量还书：逐条串行处理，每条仍是独立 IMMEDIATE 事务。

    单条失败（已归还 / 不存在 / 已下架等）不影响其余记录，逐条返回结果，
    便于柜面在一次性归还多册后看到「成功 N 册 / 失败 M 册」与失败原因。
    """
    raw_ids = payload.get("ids")
    if not isinstance(raw_ids, (list, tuple)):
        raise ValidationError("ids 必须是借阅记录 ID 数组", details={"field": "ids"})

    ids: list[int] = []
    seen: set[int] = set()
    for value in raw_ids:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"借阅记录 ID 必须是整数：{value!r}", details={"field": "ids"}) from exc
        if number <= 0:
            raise ValidationError(f"借阅记录 ID 非法：{number}", details={"field": "ids"})
        if number not in seen:  # 去重保序：同一册重复提交只还一次，避免「已归还」误报
            seen.add(number)
            ids.append(number)

    if not ids:
        raise ValidationError("请至少选择一条借阅记录", details={"field": "ids"})
    if len(ids) > MAX_BATCH_RETURN:
        raise ValidationError(
            f"单次最多批量还书 {MAX_BATCH_RETURN} 册，请分批处理", details={"field": "ids"}
        )

    succeeded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    for loan_id in ids:
        try:
            succeeded.append(return_book(loan_id))
        except AppError as exc:
            failed.append({"id": loan_id, "message": exc.message})

    return {
        "requested": len(ids),
        "succeeded_count": len(succeeded),
        "failed_count": len(failed),
        "fine_total": round(sum(float(item.get("fine") or 0) for item in succeeded), 2),
        "succeeded": succeeded,
        "failed": failed,
    }


def renew(loan_id: int) -> dict[str, Any]:
    cfg = get_config()
    loan = repo.raw(loan_id)
    if loan is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    if loan["returned_at"]:
        raise ConflictError("该借阅记录已归还，无法续借")

    # 与列表状态同口径（整日）：应还当天不算逾期，可以续借
    if fine.overdue_days(loan["due_at"]) > 0:
        raise BusinessRuleError("该图书已逾期，请先归还并缴纳罚金后再续借")

    due = timeutil.parse(loan["due_at"])
    new_due = timeutil.plus_days(due or timeutil.now(), cfg.loan_days)

    with db.transaction(immediate=True) as conn:
        # 读者状态校验放进事务：管理端续借与读者端自助续借（reader/service）口径一致，
        # 冻结读者不可续借；同时与列表 can_renew 的展示口径保持一致。
        member = member_repo.get(int(loan["member_id"]))
        if member is not None and member["status"] != "active":
            raise BusinessRuleError(f"读者「{member['name']}」已被冻结，暂时无法续借")

        if int(loan["renew_count"]) >= cfg.max_renew:
            raise BusinessRuleError(f"续借次数已达上限（{cfg.max_renew} 次）")

        # 次数上限写进 WHERE 并检查命中行数：并发续借无法突破上限
        cur = conn.execute(
            "UPDATE loans SET due_at = ?, renew_count = renew_count + 1 "
            " WHERE id = ? AND returned_at IS NULL AND renew_count < ?",
            [timeutil.to_str(new_due), loan_id, cfg.max_renew],
        )
        if cur.rowcount == 0:
            raise BusinessRuleError(f"续借次数已达上限（{cfg.max_renew} 次）")
    return get_loan(loan_id)


def _require_id(payload: Mapping[str, Any], key: str, *, label: str) -> int:
    value = payload.get(key)
    if value in (None, ""):
        raise ValidationError(f"{label} ID 不能为空", details={"field": key})
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{label} ID 必须是整数", details={"field": key}) from exc
    if number <= 0:
        raise ValidationError(f"{label} ID 非法", details={"field": key})
    return number


def loans_of_member(member_id: int) -> dict[str, Any]:
    return list_loans({"member_id": member_id, "status": "all", "page": 1, "page_size": 100})


def loans_of_book(book_id: int) -> dict[str, Any]:
    return list_loans({"book_id": book_id, "status": "all", "page": 1, "page_size": 100})
