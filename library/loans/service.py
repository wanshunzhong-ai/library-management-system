"""借阅业务逻辑：借书 / 还书 / 续借，含库存联动与并发保护。"""

from __future__ import annotations

from typing import Any, Mapping

from config import get_config
from library import db, timeutil
from library.books import repository as book_repo
from library.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from library.loans import repository as repo
from library.members import repository as member_repo
from library.validation import optional_str, pagination

STATUS_LABELS = {"borrowed": "在借", "overdue": "逾期", "returned": "已归还"}


def _decorate(row: Mapping[str, Any], *, fine_per_day: float) -> dict[str, Any]:
    """补齐派生字段：状态、逾期天数、罚金。"""
    item = dict(row)
    due = timeutil.parse(item.get("due_at"))
    returned = timeutil.parse(item.get("returned_at"))
    reference = returned or timeutil.now()

    if returned is not None:
        item["status"] = "returned"
    elif due is not None and due < timeutil.now():
        item["status"] = "overdue"
    else:
        item["status"] = "borrowed"

    overdue_days = 0
    if due is not None and reference.date() > due.date():
        overdue_days = timeutil.date_diff(reference.date(), due.date())

    item["overdue_days"] = overdue_days
    item["fine"] = round(overdue_days * fine_per_day, 2)
    item["status_label"] = STATUS_LABELS[item["status"]]
    item["can_renew"] = (
        item["status"] == "borrowed"
        and int(item.get("renew_count") or 0) < get_config().max_renew
    )
    return item


def serialize_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """供其它模块（如概览）复用的序列化入口。"""
    fine_per_day = get_config().fine_per_day
    return [_decorate(row, fine_per_day=fine_per_day) for row in rows]


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
    }
    total = repo.count(filters)
    rows = repo.list_loans(filters, limit=size, offset=(page - 1) * size)
    fine_per_day = get_config().fine_per_day
    return {
        "items": [_decorate(row, fine_per_day=fine_per_day) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
    }


def get_loan(loan_id: int) -> dict[str, Any]:
    row = repo.get(loan_id)
    if row is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    return _decorate(row, fine_per_day=get_config().fine_per_day)


def borrow(payload: Mapping[str, Any]) -> dict[str, Any]:
    cfg = get_config()
    book_id = _require_id(payload, "book_id", label="图书")
    member_id = _require_id(payload, "member_id", label="读者")
    note = optional_str(payload, "note", label="备注", max_length=200)

    member = member_repo.get(member_id)
    if member is None:
        raise NotFoundError(f"读者 #{member_id} 不存在")
    if member["status"] != "active":
        raise BusinessRuleError(f"读者「{member['name']}」已被冻结，无法借书")

    book = book_repo.get(book_id)
    if book is None:
        raise NotFoundError(f"图书 #{book_id} 不存在")

    if member_repo.overdue_count(member_id) > 0:
        raise BusinessRuleError(f"读者「{member['name']}」有逾期未还图书，请先归还")
    if member_repo.active_loan_count(member_id) >= int(member["max_borrow"]):
        raise BusinessRuleError(f"读者「{member['name']}」已达可借上限 {member['max_borrow']} 册")
    if repo.find_open_loan(book_id, member_id) is not None:
        raise ConflictError(f"读者已借阅《{book['title']}》且尚未归还")

    started = timeutil.now()
    due = timeutil.plus_days(started, cfg.loan_days)

    # 库存扣减用条件 UPDATE，避免并发下超借
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE books SET available_copies = available_copies - 1, "
            "       updated_at = datetime('now', 'localtime') "
            " WHERE id = ? AND available_copies > 0",
            [book_id],
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise BusinessRuleError(f"《{book['title']}》当前无可借库存")
        cur = conn.execute(
            "INSERT INTO loans (book_id, member_id, borrowed_at, due_at, renew_count, note) "
            "VALUES (?, ?, ?, ?, 0, ?)",
            [book_id, member_id, timeutil.to_str(started), timeutil.to_str(due), note],
        )
        loan_id = int(cur.lastrowid or 0)

    return get_loan(loan_id)


def return_book(loan_id: int) -> dict[str, Any]:
    loan = repo.raw(loan_id)
    if loan is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    if loan["returned_at"]:
        raise ConflictError("该借阅记录已归还，无需重复操作")

    with db.transaction() as conn:
        conn.execute(
            "UPDATE loans SET returned_at = ? WHERE id = ? AND returned_at IS NULL",
            [timeutil.to_str(timeutil.now()), loan_id],
        )
        conn.execute(
            "UPDATE books SET available_copies = available_copies + 1, "
            "       updated_at = datetime('now', 'localtime') "
            " WHERE id = ?",
            [loan["book_id"]],
        )

    return get_loan(loan_id)


def renew(loan_id: int) -> dict[str, Any]:
    cfg = get_config()
    loan = repo.raw(loan_id)
    if loan is None:
        raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
    if loan["returned_at"]:
        raise ConflictError("该借阅记录已归还，无法续借")

    due = timeutil.parse(loan["due_at"])
    if due is not None and due < timeutil.now():
        raise BusinessRuleError("该图书已逾期，请先归还并缴纳罚金后再续借")
    if int(loan["renew_count"]) >= cfg.max_renew:
        raise BusinessRuleError(f"续借次数已达上限（{cfg.max_renew} 次）")

    new_due = timeutil.plus_days(due or timeutil.now(), cfg.loan_days)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE loans SET due_at = ?, renew_count = renew_count + 1 "
            " WHERE id = ? AND returned_at IS NULL",
            [timeutil.to_str(new_due), loan_id],
        )
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
