"""站内通知数据访问层。

`dedupe_key` 是幂等键：同一个业务事件（同一笔借阅的这次逾期、同一条预约的到书）
只落一条，惰性扫描反复跑也不会给读者刷出一屏重复消息。
`INSERT OR IGNORE` 配合 `idx_notifications_dedupe` 这个**部分唯一索引**做兜底，
应用层以为该发、扫描器又并发跑了两次时也不会写重。
"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_COLUMNS = (
    "id, member_id, kind, title, body, link_view, ref_type, ref_id, "
    "dedupe_key, read_at, created_at"
)

_SORT_COLUMNS = {
    "id": "id",
    "kind": "kind",
    "created_at": "created_at",
}
_DEFAULT_ORDER = "id DESC"


def insert_if_absent(payload: Mapping[str, Any]) -> int:
    """写入一条通知；`dedupe_key` 已存在时静默跳过。返回新记录 id（跳过时为 0）。"""
    with db.transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO notifications "
            "(member_id, kind, title, body, link_view, ref_type, ref_id, dedupe_key) "
            "VALUES (:member_id, :kind, :title, :body, :link_view, :ref_type, :ref_id, :dedupe_key)",
            dict(payload),
        )
        created = max(0, int(cur.rowcount or 0))
        new_id = int(cur.lastrowid or 0) if created else 0
        cur.close()
    return new_id


def get(notification_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM notifications WHERE id = ?", [notification_id])


def _where(member_id: int, *, unread_only: bool, kind: str) -> tuple[str, list[Any]]:
    clauses = ["member_id = ?"]
    params: list[Any] = [member_id]
    if unread_only:
        clauses.append("read_at IS NULL")
    if kind and kind != "all":
        clauses.append("kind = ?")
        params.append(kind)
    return " WHERE " + " AND ".join(clauses), params


def count_for_member(member_id: int, *, unread_only: bool = False, kind: str = "") -> int:
    where, params = _where(member_id, unread_only=unread_only, kind=kind)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM notifications{where}", params) or 0)


def list_for_member(
    member_id: int,
    *,
    unread_only: bool = False,
    kind: str = "",
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    where, params = _where(member_id, unread_only=unread_only, kind=kind)
    sql = f"SELECT {_COLUMNS} FROM notifications{where} ORDER BY id DESC LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def kind_counts(member_id: int) -> dict[str, int]:
    """读者自己的通知按类型计数（页面上的页签角标用）。"""
    rows = db.query_all(
        "SELECT kind, COUNT(*) AS total FROM notifications WHERE member_id = ? GROUP BY kind",
        [member_id],
    )
    return {str(row["kind"]): int(row["total"]) for row in rows}


def mark_read(member_id: int, notification_id: int) -> int:
    """标记已读（只能标记自己的）。返回命中行数。"""
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at = datetime('now', 'localtime') "
            " WHERE id = ? AND member_id = ? AND read_at IS NULL",
            [notification_id, member_id],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def mark_all_read(member_id: int) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE notifications SET read_at = datetime('now', 'localtime') "
            " WHERE member_id = ? AND read_at IS NULL",
            [member_id],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed
