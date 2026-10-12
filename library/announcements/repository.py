"""公告数据访问层。

「当前生效」的判定（状态已发布 + 落在 publish_at / expire_at 窗口内）写成
一处常量片段 ``_ACTIVE_SQL``，后台列表与门户取数共用，避免两套口径各说各话。
"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

_COLUMNS = (
    "id, title, body, kind, level, pinned, status, publish_at, expire_at, "
    "operator, created_at, updated_at"
)

# 门户可见口径：已发布且处于生效窗口内
_ACTIVE_SQL = (
    "status = 'published' "
    "AND (publish_at IS NULL OR publish_at <= datetime('now', 'localtime')) "
    "AND (expire_at  IS NULL OR expire_at  >= datetime('now', 'localtime'))"
)

# 后台排序白名单（对外键 → 列表达式）
_SORT_COLUMNS = {
    "id": "id",
    "title": "title COLLATE NOCASE",
    "pinned": "pinned",
    "status": "status COLLATE NOCASE",
    "publish_at": "publish_at",
    "created_at": "created_at",
}
_DEFAULT_ORDER = "pinned DESC, COALESCE(publish_at, created_at) DESC, id DESC"


def _build_filters(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []

    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(title LIKE ? OR body LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like])

    status = (filters.get("status") or "").strip()
    if status and status != "all":
        clauses.append("status = ?")
        params.append(status)

    kind = (filters.get("kind") or "").strip()
    if kind and kind != "all":
        clauses.append("kind = ?")
        params.append(kind)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def count(filters: Mapping[str, Any]) -> int:
    where, params = _build_filters(filters)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM announcements{where}", params) or 0)


def list_all(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _build_filters(filters)
    order = db.order_clause(filters, _SORT_COLUMNS, _DEFAULT_ORDER)
    sql = f"SELECT {_COLUMNS} FROM announcements{where} ORDER BY {order} LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def get(announcement_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM announcements WHERE id = ?", [announcement_id])


def active(*, kind: str = "", limit: int = 20) -> list[dict[str, Any]]:
    """门户取数：当前生效的公告，置顶优先，其次按开始时间倒序。"""
    sql = f"SELECT {_COLUMNS} FROM announcements WHERE {_ACTIVE_SQL}"
    params: list[Any] = []
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY pinned DESC, COALESCE(publish_at, created_at) DESC, id DESC LIMIT ?"
    params.append(limit)
    return db.query_all(sql, params)


def insert(data: Mapping[str, Any]) -> int:
    return db.execute(
        """
        INSERT INTO announcements
            (title, body, kind, level, pinned, status, publish_at, expire_at, operator)
        VALUES
            (:title, :body, :kind, :level, :pinned, :status, :publish_at, :expire_at, :operator)
        """,
        dict(data),
    )


def update(announcement_id: int, data: Mapping[str, Any]) -> None:
    payload = dict(data)
    payload["id"] = announcement_id
    db.execute(
        """
        UPDATE announcements SET
            title = :title, body = :body, kind = :kind, level = :level,
            pinned = :pinned, status = :status, publish_at = :publish_at,
            expire_at = :expire_at,
            updated_at = datetime('now', 'localtime')
        WHERE id = :id
        """,
        payload,
    )


def set_status(announcement_id: int, status: str) -> int:
    """调整状态（发布 / 下线 / 撤回草稿）。返回受影响行数，供并发判定。"""
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE announcements SET status = ?, updated_at = datetime('now', 'localtime') "
            "WHERE id = ?",
            [status, announcement_id],
        )
        affected = int(cur.rowcount or 0)
        cur.close()
    return affected


def delete(announcement_id: int) -> int:
    with db.transaction() as conn:
        cur = conn.execute("DELETE FROM announcements WHERE id = ?", [announcement_id])
        affected = int(cur.rowcount or 0)
        cur.close()
    return affected
