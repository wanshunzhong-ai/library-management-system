"""赔偿处理业务逻辑：丢书 / 损书的赔付登记、结案与注销。

## 与「罚金」的区别

* **罚金**是逾期违约金，按天数算，由 ``library.fine`` 统一口径实时计算；
* **赔偿**是图书**本体**的损失（弄丢了 / 弄坏了），金额由馆员按馆内赔偿标准酌定。

两者可以并存：一本书逾期又丢了，读者既要赔书也可能要交逾期罚金。因此赔偿结案时
提供「同步减免该笔逾期罚金」的选项（一次性了结，避免读者为了赔书还要再跑一趟缴费）。

## 「按丢失结案」为什么不回补库存

正常还书会把可借数 +1；但丢书结案时这本书**已经不存在了**，如果照样回补库存，
就会出现「账上有 1 册可借、书架上却没有」的账实不符。所以这里的做法是：

* 借阅记录置为已归还（字面上是「结清这笔借阅」，不是「书回来了」）；
* **总册数 -1**，可借数按 ``总册数 - 在借数`` 重算。

数学上这一步不会改变可借数：结案前可借数 = 总 - 在借，结案后
``(总-1) - (在借-1) = 总 - 在借`` —— 少的那册本来就借在外面、本来就不在架上。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from library import db, timeutil
from library.books import repository as book_repo
from library.compensations import repository as repo
from library.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from library.loans import repository as loan_repo
from library.validation import optional_str, pagination, require_float, require_int, require_choice

logger = logging.getLogger(__name__)

KIND_LABELS = {"lost": "图书丢失", "damaged": "图书损毁"}
STATUS_LABELS = {"pending": "待结案", "settled": "已结案", "cancelled": "已注销"}
KINDS = set(KIND_LABELS)
STATUSES = set(STATUS_LABELS)

# 浮点比较容差：1 分钱以内视为相等
_EPSILON = 0.005


def _decorate(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    kind = str(item.get("kind") or "")
    status = str(item.get("status") or "")
    item["kind_label"] = KIND_LABELS.get(kind, kind)
    item["status_label"] = STATUS_LABELS.get(status, status)
    item["amount"] = round(float(item.get("amount") or 0), 2)
    item["paid_amount"] = round(float(item.get("paid_amount") or 0), 2)
    item["outstanding"] = round(max(0.0, item["amount"] - item["paid_amount"]), 2)
    # 借阅是否仍未归还：只有未归还时才谈得上「按丢失结案」
    item["loan_open"] = item.get("loan_returned_at") is None
    item["can_settle"] = status == "pending"
    item["can_cancel"] = status == "pending"
    return item


def list_compensations(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    status = (query.get("status") or "").strip()
    if status and status not in STATUSES:
        raise ValidationError(
            f"状态只能是 {'/'.join(sorted(STATUSES))} 之一", details={"field": "status"}
        )
    kind = (query.get("kind") or "").strip()
    if kind and kind not in KINDS:
        raise ValidationError(f"赔偿类型只能是 {'/'.join(sorted(KINDS))} 之一", details={"field": "kind"})

    filters = {
        "status": status,
        "kind": kind,
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
        "kind_labels": KIND_LABELS,
        "status_labels": STATUS_LABELS,
    }


def get_compensation(comp_id: int) -> dict[str, Any]:
    row = repo.get(int(comp_id))
    if row is None:
        raise NotFoundError(f"赔偿单 #{comp_id} 不存在")
    return _decorate(row)


def register(payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    """登记一笔赔付。

    校验与写入同处一个 IMMEDIATE 事务：并发下同一笔借阅不会登记出两条待结案赔付单。
    """
    loan_id = require_int(payload, "loan_id", label="借阅记录", minimum=1)
    kind = require_choice(payload, "kind", label="赔偿类型", choices=KINDS)
    amount = require_float(payload, "amount", label="赔偿金额", minimum=0.0)
    note = optional_str(payload, "note", label="备注", max_length=200)
    if amount <= 0:
        raise ValidationError("赔偿金额必须大于 0", details={"field": "amount"})

    with db.transaction(immediate=True) as conn:
        loan = loan_repo.raw(loan_id)
        if loan is None:
            raise NotFoundError(f"借阅记录 #{loan_id} 不存在")
        # 丢书必然还没还；损毁既可能在借（还没还）也可能已经还了（还书时发现破损）
        if kind == "lost" and loan.get("returned_at"):
            raise BusinessRuleError("该借阅记录已归还，无法按「图书丢失」登记；如为还书后发现破损请改选「图书损毁」")
        if repo.find_open_by_loan(loan_id) is not None:
            raise ConflictError("该借阅记录已有待结案的赔偿单，请先结案或注销")

        comp_id = repo.insert(
            conn,
            {
                "loan_id": loan_id,
                "member_id": int(loan["member_id"]),
                "book_id": int(loan["book_id"]),
                "kind": kind,
                "amount": round(float(amount), 2),
                "note": note,
                "operator": operator,
            },
        )

    logger.info(
        "登记图书赔偿",
        extra={"extra_fields": {"compensation_id": comp_id, "loan_id": loan_id, "amount": amount}},
    )
    return get_compensation(comp_id)


def settle(comp_id: int, payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    """赔偿结案：记账 + 可选「按丢失结案」+ 可选「同步减免逾期罚金」。"""
    comp_id = int(comp_id)
    note = optional_str(payload, "note", label="备注", max_length=200)
    close_loan = _as_bool(payload.get("close_loan"))
    waive_fine = _as_bool(payload.get("waive_fine"))

    with db.transaction(immediate=True) as conn:
        row = repo.get_for_update(conn, comp_id)
        if row is None:
            raise NotFoundError(f"赔偿单 #{comp_id} 不存在")
        if row["status"] != "pending":
            raise ConflictError(f"该赔偿单已是「{STATUS_LABELS.get(str(row['status']), row['status'])}」，无法重复结案")

        amount = round(float(row["amount"]), 2)
        raw_paid = payload.get("paid_amount")
        if raw_paid in (None, ""):
            paid = amount
        else:
            try:
                paid = round(float(raw_paid), 2)
            except (TypeError, ValueError) as exc:
                raise ValidationError("实收金额必须是数字", details={"field": "paid_amount"}) from exc
            if paid <= 0:
                raise ValidationError("实收金额必须大于 0", details={"field": "paid_amount"})
            if paid > amount + _EPSILON:
                raise BusinessRuleError(f"实收金额不能超过应赔金额 ¥{amount:.2f}")

        book_id = int(row["book_id"])
        member_id = int(row["member_id"])
        loan_id = int(row["loan_id"])

        if close_loan:
            cur = conn.execute(
                "UPDATE loans SET returned_at = ? WHERE id = ? AND returned_at IS NULL",
                [timeutil.to_str(timeutil.now()), loan_id],
            )
            closed = max(0, int(cur.rowcount or 0))
            cur.close()
            if closed == 0:
                raise BusinessRuleError("该借阅记录已归还，无法再按丢失结案（请取消勾选后重试）")
            # 不回补库存：书已不在架上，改为把总册数 -1；可借数按不变式重算
            book = book_repo.get(book_id)
            if book is None:
                raise NotFoundError(f"图书 #{book_id} 不存在")
            total = int(book["total_copies"])
            active = _open_loans(conn, book_id)
            new_total = total - 1
            if new_total < active:
                raise BusinessRuleError(
                    f"《{book['title']}》按丢失核销后总册数将小于在借册数（{active} 册），请先处理在借记录"
                )
            cur = conn.execute(
                "UPDATE books SET total_copies = ?, available_copies = ?, "
                "       updated_at = datetime('now', 'localtime') WHERE id = ?",
                [new_total, new_total - active, book_id],
            )
            cur.close()

        waived = 0.0
        if waive_fine:
            from library.fines import service as fine_service  # noqa: PLC0415

            waived = fine_service.waive_for_loan(
                conn,
                member_id,
                loan_id,
                reason=f"图书赔偿结案（#{comp_id}）",
                operator=operator,
            )

        if not repo.mark_settled(
            conn, comp_id, paid_amount=paid, note=note, operator=operator
        ):
            raise ConflictError("该赔偿单状态已变化，请刷新后重试")

    logger.info(
        "赔偿结案",
        extra={
            "extra_fields": {
                "compensation_id": comp_id,
                "paid": paid,
                "close_loan": close_loan,
                "waived_fine": waived,
            }
        },
    )
    result = get_compensation(comp_id)
    result["closed_loan"] = bool(close_loan)
    result["waived_fine"] = round(waived, 2)
    return result


def cancel(comp_id: int, payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    """注销待结案的赔偿单（登记错了 / 读者找回图书）。仅待结案可注销。"""
    comp_id = int(comp_id)
    note = optional_str(payload, "note", label="备注", max_length=200)
    with db.transaction(immediate=True) as conn:
        row = repo.get_for_update(conn, comp_id)
        if row is None:
            raise NotFoundError(f"赔偿单 #{comp_id} 不存在")
        if row["status"] != "pending":
            raise ConflictError("只有「待结案」的赔偿单可以注销")
        if not repo.mark_cancelled(conn, comp_id, note=note, operator=operator):
            raise ConflictError("该赔偿单状态已变化，请刷新后重试")
    logger.info("注销赔偿单", extra={"extra_fields": {"compensation_id": comp_id}})
    return get_compensation(comp_id)


def pending_of_member(member_id: int) -> dict[str, Any]:
    return {
        "items": [_decorate(row) for row in repo.list_pending_for_member(int(member_id))],
    }


def _open_loans(conn, book_id: int) -> int:
    """事务内统计该书未归还册数（只用调用方连接，保证读到的是本事务的最新状态）。"""
    cur = conn.execute(
        "SELECT COUNT(*) FROM loans WHERE book_id = ? AND returned_at IS NULL", [book_id]
    )
    try:
        row = cur.fetchone()
        return int(row[0]) if row is not None else 0
    finally:
        cur.close()


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
