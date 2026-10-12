"""审批工单仓储。"""

from __future__ import annotations

import json
from typing import Any, Mapping

from library import db

_SELECT = """
SELECT a.*, m.name AS member_name, m.card_no
  FROM approvals a
  LEFT JOIN members m ON m.id = a.member_id
"""


def _decorate(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    try:
        item["payload_data"] = json.loads(item.get("payload") or "") if item.get("payload") else {}
    except (ValueError, TypeError):
        item["payload_data"] = {}
    return item


def list_rows(filters: Mapping[str, Any], *, limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    where = ["1=1"]
    params: list[Any] = []
    status = str(filters.get("status") or "").strip()
    if status in ("pending", "approved", "rejected"):
        where.append("a.status = ?")
        params.append(status)
    keyword = str(filters.get("keyword") or "").strip()
    if keyword:
        where.append("(a.entity_label LIKE ? OR a.requested_by LIKE ? OR m.name LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like])
    clause = " AND ".join(where)
    total = int(
        (
            db.query_one(
                f"SELECT COUNT(*) AS n FROM approvals a LEFT JOIN members m ON m.id = a.member_id WHERE {clause}",
                params,
            )
            or {"n": 0}
        )["n"]
    )
    rows = db.query_all(
        f"{_SELECT} WHERE {clause} ORDER BY a.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return [_decorate(r) for r in rows], total


def get(approval_id: int) -> dict[str, Any] | None:
    row = db.query_one(f"{_SELECT} WHERE a.id = ?", [int(approval_id)])
    return _decorate(row) if row else None


def get_for_update(conn, approval_id: int) -> dict[str, Any] | None:
    cur = conn.execute("SELECT * FROM approvals WHERE id = ?", [int(approval_id)])
    row = cur.fetchone()
    cur.close()
    return _decorate(dict(row)) if row else None


def insert_conn(
    conn,
    *,
    kind: str,
    entity_id: int | None,
    entity_label: str,
    member_id: int | None,
    amount: float,
    payload: Mapping[str, Any],
    requested_by: str,
    note: str,
) -> int:
    import json as _json  # noqa: PLC0415

    cur = conn.execute(
        "INSERT INTO approvals (kind, entity_id, entity_label, member_id, amount, payload, "
        "requested_by, requested_at, note, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'), ?, 'pending')",
        [
            kind,
            entity_id,
            entity_label,
            member_id,
            amount,
            _json.dumps(dict(payload), ensure_ascii=False),
            requested_by,
            note,
        ],
    )
    new_id = int(cur.lastrowid or 0)
    cur.close()
    return new_id


def mark_reviewed(conn, approval_id: int, *, status: str, reviewed_by: str, review_note: str) -> None:
    cur = conn.execute(
        "UPDATE approvals SET status = ?, reviewed_by = ?, reviewed_at = datetime('now', 'localtime'), "
        "review_note = ? WHERE id = ?",
        [status, reviewed_by, review_note, int(approval_id)],
    )
    cur.close()


def pending_count() -> int:
    row = db.query_one("SELECT COUNT(*) AS n FROM approvals WHERE status = 'pending'")
    return int((row or {"n": 0})["n"])
