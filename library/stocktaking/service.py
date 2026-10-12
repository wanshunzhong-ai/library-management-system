"""盘点业务逻辑：开单快照 → 实盘录入 → 差异报告 → 关闭冻结 → 按差异调账。

## 为什么开单要「快照」

盘点比的不是「现在库里有几册」，而是「**开单那一刻**账上有几册，实际在架几册」。
如果每次读实时库存，盘的过程中有人借书还书，账就一直在动，差异报告会变成
「与刚刚那一刻的对比」，没法追溯。所以开单时把应盘册数写死进明细里。

## 为什么调账要单独确认

盘点只负责**发现**差异（可能只是书放错架、条码没扫上），改账是另一回事。
把两者分开，是为了让馆员先看完差异报告、确认差异真实存在，再决定要不要把
账改成实盘数——避免「盘错一次就把库存账改坏」。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from library import db, timeutil
from library.errors import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from library.stocktaking import repository as repo
from library.validation import optional_int, optional_str, pagination

logger = logging.getLogger(__name__)

STATUS_LABELS = {"open": "进行中", "closed": "已关闭"}
ALL_SCOPE_LABEL = "全馆"


def _decorate_batch(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    status = str(item.get("status") or "")
    item["status_label"] = STATUS_LABELS.get(status, status)
    item["location_label"] = str(item.get("location") or "") or ALL_SCOPE_LABEL
    total = int(item.get("item_count") or 0)
    counted = int(item.get("counted_count") or 0)
    item["counted_count"] = counted
    item["diff_count"] = int(item.get("diff_count") or 0)
    item["pending_count"] = max(0, total - counted)
    item["progress"] = round(counted / total * 100, 1) if total else 0.0
    item["is_open"] = status == "open"
    return item


def _diff_of(row: Mapping[str, Any]) -> int | None:
    counted = row.get("counted_copies")
    if counted is None:
        return None
    return int(counted) - int(row.get("expected_copies") or 0)


def _decorate_item(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    diff = _diff_of(row)
    item["diff"] = diff
    if diff is None:
        item["state"] = "pending"
        item["state_label"] = "未盘"
    elif diff > 0:
        item["state"] = "up"
        item["state_label"] = f"盘盈 {diff}"
    elif diff < 0:
        item["state"] = "down"
        item["state_label"] = f"盘亏 {abs(diff)}"
    else:
        item["state"] = "match"
        item["state_label"] = "账实一致"
    return item


def list_batches(query: Mapping[str, Any]) -> dict[str, Any]:
    page, size = pagination(query)
    status = (query.get("status") or "").strip()
    if status and status not in STATUS_LABELS:
        raise ValidationError("状态只能是 open/closed 之一", details={"field": "status"})
    filters = {
        "status": status,
        "location": query.get("location", ""),
        "keyword": query.get("keyword", ""),
    }
    total = repo.count_batches(filters)
    rows = repo.list_batches(filters, limit=size, offset=(page - 1) * size)
    return {
        "items": [_decorate_batch(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": size,
        "pages": max(1, -(-total // size)),
        "locations": repo.locations(),
        "status_labels": STATUS_LABELS,
    }


def get_batch(batch_id: int, query: Mapping[str, Any] | None = None) -> dict[str, Any]:
    batch_id = int(batch_id)
    row = repo.get_batch(batch_id)
    if row is None:
        raise NotFoundError(f"盘点单 #{batch_id} 不存在")
    params = query or {}
    state = (params.get("state") or "").strip()
    items = [_decorate_item(r) for r in repo.list_items(batch_id)]
    if state:
        if state not in {"pending", "up", "down", "match"}:
            raise ValidationError("差异筛选不合法", details={"field": "state"})
        items = [item for item in items if item["state"] == state]

    expected_total = sum(int(item.get("expected_copies") or 0) for item in items)
    counted_total = sum(
        int(item["counted_copies"]) for item in items if item.get("counted_copies") is not None
    )
    summary = {
        "items": len(items),
        "counted": sum(1 for item in items if item["state"] != "pending"),
        "pending": sum(1 for item in items if item["state"] == "pending"),
        "matched": sum(1 for item in items if item["state"] == "match"),
        "up": sum(1 for item in items if item["state"] == "up"),
        "down": sum(1 for item in items if item["state"] == "down"),
        "expected_total": expected_total,
        "counted_total": counted_total,
        "diff_total": counted_total - sum(
            int(item.get("expected_copies") or 0)
            for item in items
            if item.get("counted_copies") is not None
        ),
    }
    batch = _decorate_batch(row)
    batch["summary"] = summary
    batch["items"] = items
    return batch


def create_batch(payload: Mapping[str, Any], *, operator: str = "") -> dict[str, Any]:
    """开一张盘点单：按馆藏位置（留空 = 全馆）把图书快照进明细。"""
    location = optional_str(payload, "location", label="馆藏位置", max_length=50)
    note = optional_str(payload, "note", label="备注", max_length=200)

    with db.transaction(immediate=True) as conn:
        existing = repo.find_open_location(location)
        if existing is not None:
            label = location or ALL_SCOPE_LABEL
            raise ConflictError(
                f"「{label}」还有进行中的盘点单（{existing['code']}），请先关闭或删除后再开新单",
                details={"field": "location"},
            )
        code = _next_code(conn)
        batch_id = repo.insert_batch(
            conn,
            {"code": code, "location": location, "note": note, "operator": operator},
        )
        created = repo.snapshot_items(conn, batch_id, location)
        if created == 0:
            raise BusinessRuleError(
                "该范围内没有可盘点的图书" + (f"（馆藏位置：{location}）" if location else "")
            )
        cur = conn.execute(
            "UPDATE stocktaking_batches SET item_count = ?, "
            "       updated_at = datetime('now', 'localtime') WHERE id = ?",
            [created, batch_id],
        )
        cur.close()

    logger.info(
        "开启盘点单",
        extra={"extra_fields": {"batch_id": batch_id, "code": code, "location": location, "items": created}},
    )
    return get_batch(batch_id)


def set_counted(batch_id: int, item_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """录入某本书的实盘册数（留空 = 取消盘点该项）。"""
    batch_id = int(batch_id)
    item_id = int(item_id)
    note = optional_str(payload, "note", label="备注", max_length=200)
    raw = payload.get("counted_copies")
    if raw in (None, ""):
        counted: int | None = None
    else:
        counted = optional_int(payload, "counted_copies", label="实盘册数", minimum=0, maximum=9999)

    with db.transaction(immediate=True) as conn:
        _require_open_batch(conn, batch_id)
        row = repo.item(batch_id, item_id)
        if row is None:
            raise NotFoundError(f"盘点明细 #{item_id} 不存在")
        if not repo.set_item_count(conn, batch_id, item_id, counted, note):
            raise ConflictError("该盘点明细已变化，请刷新后重试")
        repo.touch_batch(conn, batch_id)

    return get_batch(batch_id)


def close_batch(batch_id: int) -> dict[str, Any]:
    """关闭盘点单：冻结明细，之后只能调账或删除，不能继续改实盘数。"""
    batch_id = int(batch_id)
    with db.transaction(immediate=True) as conn:
        row = repo.get_batch(batch_id)
        if row is None:
            raise NotFoundError(f"盘点单 #{batch_id} 不存在")
        if row["status"] != "open":
            raise ConflictError("该盘点单已关闭")
        if not repo.close_batch(conn, batch_id):
            raise ConflictError("该盘点单状态已变化，请刷新后重试")
    logger.info("关闭盘点单", extra={"extra_fields": {"batch_id": batch_id}})
    return get_batch(batch_id)


def reopen_batch(batch_id: int) -> dict[str, Any]:
    """重新打开盘点单（发现漏盘时用）。"""
    batch_id = int(batch_id)
    with db.transaction(immediate=True) as conn:
        row = repo.get_batch(batch_id)
        if row is None:
            raise NotFoundError(f"盘点单 #{batch_id} 不存在")
        if row["status"] != "closed":
            raise ConflictError("该盘点单本身就是进行中")
        if not repo.reopen_batch(conn, batch_id):
            raise ConflictError("该盘点单状态已变化，请刷新后重试")
    logger.info("重新打开盘点单", extra={"extra_fields": {"batch_id": batch_id}})
    return get_batch(batch_id)


def apply_batch(batch_id: int, payload: Mapping[str, Any]) -> dict[str, Any]:
    """按差异调账：把实盘数写回总册数（**必须先关闭盘点单**，且不得低于在借册数）。

    ``only_diff``（默认 true）只处理「盘过且与应盘不符」的项；传 false 则把所有
    已盘项（含一致的）都写一遍（幂等，用于「这本书的总册数在盘点期间被别处改过」
    这种边角情况）。
    """
    batch_id = int(batch_id)
    only_diff = str(payload.get("only_diff", "true")).strip().lower() not in {"0", "false", "no"}

    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    with db.transaction(immediate=True) as conn:
        batch = repo.get_batch(batch_id)
        if batch is None:
            raise NotFoundError(f"盘点单 #{batch_id} 不存在")
        if batch["status"] != "closed":
            raise BusinessRuleError("请先关闭盘点单，确认差异真实存在后再调账")

        rows = repo.list_items(batch_id, only_counted=True)
        targets = [r for r in rows if not only_diff or _diff_of(r) != 0]
        if not targets:
            return {
                "batch_id": batch_id,
                "applied": 0,
                "skipped": 0,
                "details": [],
                "message": "没有需要调整的差异",
            }

        # 先全量校验再写：任何一项不合法都不应产生半截调账
        for row in targets:
            counted = int(row["counted_copies"])
            active = repo.open_loans_for_book(conn, int(row["book_id"]))
            if counted < active:
                raise BusinessRuleError(
                    f"《{row['book_title']}》实盘 {counted} 册，但当前有 {active} 册在外借阅，"
                    "无法把总册数调到低于在借册数（请先处理在借记录）"
                )

        for row in targets:
            counted = int(row["counted_copies"])
            active = repo.open_loans_for_book(conn, int(row["book_id"]))
            cur = conn.execute(
                "UPDATE books SET total_copies = ?, available_copies = ?, "
                "       updated_at = datetime('now', 'localtime') WHERE id = ?",
                [counted, counted - active, int(row["book_id"])],
            )
            changed = max(0, int(cur.rowcount or 0))
            cur.close()
            detail = {
                "book_id": int(row["book_id"]),
                "title": row["book_title"],
                "barcode": row.get("book_barcode") or "",
                "before": int(row["expected_copies"] or 0),
                "after": counted,
                "diff": _diff_of(row),
            }
            (applied if changed else skipped).append(detail)

    logger.info(
        "盘点差异调账",
        extra={"extra_fields": {"batch_id": batch_id, "applied": len(applied), "skipped": len(skipped)}},
    )
    return {
        "batch_id": batch_id,
        "applied": len(applied),
        "skipped": len(skipped),
        "details": applied + skipped,
        "message": f"已按实盘数调整 {len(applied)} 种图书的馆藏数量",
    }


def delete_batch(batch_id: int) -> dict[str, Any]:
    """删除盘点单（仅进行中的可删；已关闭的单保留作历史记录）。"""
    batch_id = int(batch_id)
    with db.transaction(immediate=True) as conn:
        row = repo.get_batch(batch_id)
        if row is None:
            raise NotFoundError(f"盘点单 #{batch_id} 不存在")
        if row["status"] != "open":
            raise ConflictError("已关闭的盘点单不可删除（保留作盘点记录）")
    repo.delete_batch(batch_id)
    logger.info("删除盘点单", extra={"extra_fields": {"batch_id": batch_id}})
    return {"deleted": batch_id, "code": str(row["code"])}


def _require_open_batch(conn, batch_id: int) -> dict[str, Any]:
    row = repo.get_batch(batch_id)
    if row is None:
        raise NotFoundError(f"盘点单 #{batch_id} 不存在")
    if row["status"] != "open":
        raise ConflictError("盘点单已关闭，无法继续录入实盘数（可先重新打开）")
    return row


def _next_code(conn) -> str:
    """盘点单号：PD + 日期 + 当日序号（如 PD20261011-002）。"""
    prefix = "PD" + timeutil.now().strftime("%Y%m%d") + "-"
    cur = conn.execute(
        "SELECT COUNT(*) FROM stocktaking_batches WHERE code LIKE ?", [f"{prefix}%"]
    )
    try:
        row = cur.fetchone()
        used = int(row[0]) if row is not None else 0
    finally:
        cur.close()
    for seq in range(used + 1, used + 200):
        candidate = f"{prefix}{seq:03d}"
        probe = conn.execute("SELECT 1 FROM stocktaking_batches WHERE code = ?", [candidate])
        try:
            exists = probe.fetchone() is not None
        finally:
            probe.close()
        if not exists:
            return candidate
    raise ConflictError("当日盘点单号已用尽，请手工清理历史记录")
