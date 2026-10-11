"""滞纳金业务逻辑：应缴计算（统一口径）、自助缴纳、馆员代收 / 减免、缴纳历史。

**应缴金额不落库**，每次都按 ``library.fine`` 的统一口径（整日 + 封顶）由借阅记录
实时算出，再减去已结算（缴纳 + 减免）的金额——避免把派生数据抄成第二份后与
逾期天数脱节（这正是评审里 P1-4/P1-5 踩过的坑）。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from config import get_config
from library import db, fine
from library.errors import BusinessRuleError, NotFoundError, ValidationError
from library.fines import repository as repo
from library.validation import optional_str, pagination

logger = logging.getLogger(__name__)

KIND_LABELS = {"payment": "缴纳", "waiver": "减免"}
METHOD_LABELS = {"self": "自助缴纳", "cash": "现金", "online": "线上", "waive": "减免"}
# 结清判定阈值：浮点求和后小于 1 分钱即视为已结清
_EPSILON = 0.005


def _decorate_payment(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["amount"] = round(float(item.get("amount") or 0), 2)
    item["kind_label"] = KIND_LABELS.get(str(item.get("kind") or ""), str(item.get("kind") or ""))
    method = str(item.get("method") or "")
    item["method_label"] = METHOD_LABELS.get(method, method or "—")
    return item


def _outstanding_items(member_id: int) -> list[dict[str, Any]]:
    """当前仍待缴的明细（逐笔借阅：逾期罚金 − 已结算）。

    在借逾期与已归还逾期都会列出；已结清 / 未逾期的不列。
    """
    settled = repo.settled_by_loan(member_id)
    rows = db.query_all(
        "SELECT l.id, l.book_id, l.borrowed_at, l.due_at, l.returned_at, "
        "       b.title AS book_title, b.barcode AS book_barcode, b.isbn AS book_isbn "
        "  FROM loans l JOIN books b ON b.id = l.book_id "
        " WHERE l.member_id = ? ORDER BY l.id DESC",
        [member_id],
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        settlement = fine.loan_settlement(row.get("due_at"), row.get("returned_at"))
        accrued = float(settlement["fine"])
        if accrued <= 0:
            continue
        loan_id = int(row["id"])
        already = float(settled.get(loan_id, 0.0))
        owed = round(accrued - already, 2)
        if owed <= _EPSILON:
            continue
        if row.get("returned_at"):
            state = "returned"
        elif settlement["days"] > 0:
            state = "overdue"
        else:
            state = "borrowed"
        items.append(
            {
                "loan_id": loan_id,
                "book_id": int(row["book_id"]),
                "book_title": row.get("book_title") or "",
                "book_barcode": row.get("book_barcode") or "",
                "book_isbn": row.get("book_isbn") or "",
                "borrowed_at": row.get("borrowed_at"),
                "due_at": row.get("due_at"),
                "returned_at": row.get("returned_at"),
                "overdue_days": int(settlement["days"]),
                "fine": round(accrued, 2),
                "settled": round(already, 2),
                "outstanding": owed,
                "capped": bool(settlement["capped"]),
                "state": state,
                "state_label": {"returned": "已归还", "overdue": "在借逾期", "borrowed": "在借"}[state],
            }
        )
    return items


def overview(member: Mapping[str, Any]) -> dict[str, Any]:
    """读者端「滞纳金」总览：待缴明细 + 累计已缴 / 减免 + 缴纳历史。"""
    member_id = int(member["id"])
    items = _outstanding_items(member_id)
    totals = repo.summary_for_member(member_id)
    history = [_decorate_payment(row) for row in repo.history_for_member(member_id, limit=20)]
    return {
        "items": items,
        "total_outstanding": round(sum(item["outstanding"] for item in items), 2),
        "total_paid": totals["paid"],
        "total_waived": totals["waived"],
        "history": history,
        "rules": _fine_rules(),
    }


def _fine_rules() -> dict[str, Any]:
    cfg = get_config()
    return {
        "fine_per_day": cfg.fine_per_day,
        "fine_max_days": cfg.fine_max_days,
        "fine_cap": cfg.fine_cap,
    }


def pay(member: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    """读者自助缴纳：可按单笔或一次结清全部待缴。

    校验与写入在同一个 IMMEDIATE 事务里完成，避免并发下重复缴纳同一笔。
    """
    member_id = int(member["id"])
    method = optional_str(payload, "method", label="缴纳方式", max_length=20) or "self"
    note = optional_str(payload, "note", label="备注", max_length=200)
    pay_all = bool(payload.get("all"))
    raw_loan_id = payload.get("loan_id")
    if not pay_all and raw_loan_id in (None, ""):
        raise ValidationError("请选择要缴纳的借阅记录，或选择一次结清", details={"field": "loan_id"})
    try:
        loan_id = int(raw_loan_id) if raw_loan_id not in (None, "") else None
    except (TypeError, ValueError) as exc:
        raise ValidationError("借阅记录 ID 必须是整数", details={"field": "loan_id"}) from exc

    operator = f"{member.get('name') or ''}({member.get('card_no') or ''})"
    with db.transaction(immediate=True) as conn:
        items = _outstanding_items(member_id)
        if not items:
            raise BusinessRuleError("当前没有需要缴纳的滞纳金")
        if pay_all:
            targets = items
        else:
            targets = [item for item in items if item["loan_id"] == loan_id]
            if not targets:
                raise NotFoundError("该借阅记录没有待缴的滞纳金（可能已结清或未逾期）")

        for item in targets:
            cur = conn.execute(
                "INSERT INTO fine_payments (member_id, loan_id, amount, kind, method, note, operator) "
                "VALUES (?, ?, ?, 'payment', ?, ?, ?)",
                [member_id, item["loan_id"], item["outstanding"], method, note, operator],
            )
            cur.close()

    paid_total = round(sum(item["outstanding"] for item in targets), 2)
    logger.info(
        "读者自助缴纳滞纳金",
        extra={"extra_fields": {"member_id": member_id, "count": len(targets), "amount": paid_total}},
    )
    return {
        "paid_total": paid_total,
        "count": len(targets),
        "overview": overview(member),
        "message": f"已缴纳 ¥{paid_total:.2f}，共 {len(targets)} 笔",
    }


# ---------------------------------------------------------------- 馆员侧


def list_payments(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query, default_size=20, max_size=100)
    filters = {"kind": (query.get("kind") or "").strip(), "keyword": query.get("keyword", "")}
    total = repo.count_all(filters)
    rows = repo.list_all(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_decorate_payment(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "totals": repo.totals(filters),
    }


def member_outstanding(member_id: int) -> dict[str, Any]:
    items = _outstanding_items(int(member_id))
    return {
        "items": items,
        "total_outstanding": round(sum(item["outstanding"] for item in items), 2),
    }


def settle(payload: Mapping[str, Any], *, kind: str, operator: str) -> dict[str, Any]:
    """馆员代收（payment）或减免（waiver）。amount 缺省表示结清该笔全部应缴。"""
    member_id = _require_int(payload, "member_id", label="读者")
    loan_id = _require_int(payload, "loan_id", label="借阅记录")
    note = optional_str(payload, "note", label="备注", max_length=200)
    method = optional_str(payload, "method", label="方式", max_length=20) or ("cash" if kind == "payment" else "waive")

    with db.transaction(immediate=True) as conn:
        items = _outstanding_items(member_id)
        target = next((item for item in items if item["loan_id"] == loan_id), None)
        if target is None:
            raise BusinessRuleError("该借阅记录没有待结算的滞纳金")
        owed = float(target["outstanding"])
        raw_amount = payload.get("amount")
        if raw_amount in (None, ""):
            amount = owed
        else:
            try:
                amount = round(float(raw_amount), 2)
            except (TypeError, ValueError) as exc:
                raise ValidationError("金额必须是数字", details={"field": "amount"}) from exc
            if amount <= 0:
                raise ValidationError("金额必须大于 0", details={"field": "amount"})
            if amount > owed + _EPSILON:
                raise BusinessRuleError(f"金额不能超过该笔应缴 ¥{owed:.2f}")

        cur = conn.execute(
            "INSERT INTO fine_payments (member_id, loan_id, amount, kind, method, note, operator) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [member_id, loan_id, amount, kind, method, note, operator],
        )
        cur.close()

    logger.info(
        "馆员结算滞纳金",
        extra={"extra_fields": {"member_id": member_id, "loan_id": loan_id, "kind": kind, "amount": amount}},
    )
    return {"member_id": member_id, "loan_id": loan_id, "amount": amount, "kind": kind}


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
