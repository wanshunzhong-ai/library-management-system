"""审批工单服务：罚金减免超阈值时「先提交、后批准」。

## 流程

1. 馆员在「滞纳金管理」里提交一笔减免。``fines.service.settle`` 在同一事务里
   调用 :func:`maybe_defer_waiver`：金额达到阈值（``LIBRARY_APPROVAL_WAIVER_THRESHOLD``，
   默认 0 = 不启用审批、直接生效）时**不执行减免**，只落一张 ``pending`` 工单。
2. 系统管理员在「审批中心」通过 / 驳回。**通过才真正执行**减免（同一事务内），
   驳回只留意见，业务数据零改动。
3. 全程留痕：谁申请、谁批准、批注是什么，一张工单看全。

## 为什么阈值默认 0

不启用审批时行为与历史版本完全一致（减免直接生效）——对只有一两名馆员的小馆，
强制走审批是自我添堵；但对减免频次高、金额敏感的馆，把大额减免交给馆长把关
是真实的内控需求。阈值就是一个开关。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from library import db
from library.approvals import repository as repo
from library.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from library.validation import optional_str, require_int

logger = logging.getLogger(__name__)

KIND_LABELS: dict[str, str] = {"fine_waiver": "罚金减免"}
STATUS_LABELS: dict[str, str] = {"pending": "待审批", "approved": "已通过", "rejected": "已驳回"}

MAX_LIST_LIMIT = 100


def _threshold() -> float:
    from config import get_config  # noqa: PLC0415

    return float(get_config().approval_waiver_threshold or 0)


# ---------------------------------------------------------------- 提交（由罚金模块在事务内调用）


def maybe_defer_waiver(
    conn,
    member_id: int,
    loan_id: int,
    amount: float,
    *,
    note: str,
    operator: str,
) -> dict[str, Any] | None:
    """减免金额达到阈值时在**调用方事务内**落一张待审批工单；未启用 / 未达阈值返回 None。

    必须接收调用方连接（``conn.execute``）——工单与「不执行减免」这个决定必须同生共死。
    """
    threshold = _threshold()
    if threshold <= 0 or float(amount) < threshold:
        return None

    row = conn.execute(
        "SELECT m.name AS member_name, m.card_no, b.title AS book_title "
        "FROM members m, books b WHERE m.id = ? AND b.id = (SELECT book_id FROM loans WHERE id = ?)",
        [int(member_id), int(loan_id)],
    ).fetchone()
    meta = dict(row) if row else {}
    reader = meta.get("member_name") or f"读者#{member_id}"
    title = meta.get("book_title") or ""
    label = f"减免 {reader}《{title}》滞纳金 ¥{float(amount):.2f}（借阅 #{loan_id}）"

    approval_id = repo.insert_conn(
        conn,
        kind="fine_waiver",
        entity_id=int(loan_id),
        entity_label=label,
        member_id=int(member_id),
        amount=round(float(amount), 2),
        payload={"member_id": int(member_id), "loan_id": int(loan_id), "amount": float(amount), "note": note},
        requested_by=operator,
        note=note,
    )
    logger.info(
        "罚金减免已转审批",
        extra={"extra_fields": {"approval_id": approval_id, "amount": amount, "threshold": threshold, "operator": operator}},
    )
    return {
        "kind": "waiver",
        "status": "pending_approval",
        "approval_id": approval_id,
        "amount": round(float(amount), 2),
        "threshold": round(threshold, 2),
        "message": f"减免金额达到审批阈值 ¥{threshold:.2f}，已提交审批，待系统管理员通过后生效",
    }


# ---------------------------------------------------------------- 查询


def list_approvals(query: Mapping[str, Any]) -> dict[str, Any]:
    try:
        page = max(1, int(query.get("page") or 1))
    except (TypeError, ValueError):
        page = 1
    try:
        page_size = min(MAX_LIST_LIMIT, max(1, int(query.get("page_size") or 20)))
    except (TypeError, ValueError):
        page_size = 20
    rows, total = repo.list_rows(query, limit=page_size, offset=(page - 1) * page_size)
    items = []
    for row in rows:
        item = dict(row)
        item["kind_label"] = KIND_LABELS.get(str(row.get("kind") or ""), str(row.get("kind") or ""))
        item["status_label"] = STATUS_LABELS.get(str(row.get("status") or ""), str(row.get("status") or ""))
        items.append(item)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": max(1, -(-total // page_size)),
        "pending": repo.pending_count(),
        "status_labels": STATUS_LABELS,
    }


def pending_count() -> int:
    return repo.pending_count()


# ---------------------------------------------------------------- 审批


def approve(approval_id: int, payload: Mapping[str, Any], *, operator: str) -> dict[str, Any]:
    """通过工单并**在同一事务内**执行对应业务（目前只有罚金减免）。"""
    review_note = optional_str(payload, "review_note", label="审批意见", max_length=200)
    with db.transaction(immediate=True) as conn:
        approval = repo.get_for_update(conn, int(approval_id))
        if approval is None:
            raise NotFoundError("审批工单不存在")
        if str(approval["status"]) != "pending":
            # 「状态已变、不能重复处理」是本项目一贯的 409 口径（同申请单审核、验收单结项），
            # 不是业务规则不满足，语义上属于资源状态冲突。
            raise ConflictError("该工单已审批完毕，不能重复处理")

        kind = str(approval["kind"])
        if kind != "fine_waiver":
            raise BusinessRuleError(f"未知的审批类型：{kind}")

        from library.fines import service as fine_service  # noqa: PLC0415

        data = approval.get("payload_data") or {}
        member_id = int(data.get("member_id") or approval.get("member_id") or 0)
        loan_id = int(data.get("loan_id") or approval.get("entity_id") or 0)
        amount = float(data.get("amount") or approval.get("amount") or 0)
        note = str(data.get("note") or approval.get("note") or "")
        executed = fine_service.execute_approved_waiver(conn, member_id, loan_id, amount, reason=note, operator=operator)

        repo.mark_reviewed(
            conn, int(approval_id), status="approved", reviewed_by=operator, review_note=review_note
        )
    logger.info(
        "审批通过并执行",
        extra={"extra_fields": {"approval_id": approval_id, "kind": kind, "waived": executed, "operator": operator}},
    )
    return {
        "id": int(approval_id),
        "status": "approved",
        "waived_amount": executed,
        "message": f"审批通过，已减免滞纳金 ¥{executed:.2f}",
    }


def reject(approval_id: int, payload: Mapping[str, Any], *, operator: str) -> dict[str, Any]:
    """驳回工单：意见必填，业务数据零改动。"""
    review_note = optional_str(payload, "review_note", label="审批意见", max_length=200)
    if not review_note:
        raise ValidationError("驳回必须填写审批意见", details={"field": "review_note"})
    with db.transaction(immediate=True) as conn:
        approval = repo.get_for_update(conn, int(approval_id))
        if approval is None:
            raise NotFoundError("审批工单不存在")
        if str(approval["status"]) != "pending":
            raise ConflictError("该工单已审批完毕，不能重复处理")
        repo.mark_reviewed(conn, int(approval_id), status="rejected", reviewed_by=operator, review_note=review_note)
    logger.info("审批驳回", extra={"extra_fields": {"approval_id": approval_id, "operator": operator}})
    return {"id": int(approval_id), "status": "rejected"}
