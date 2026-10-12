"""预约数据访问层。

状态流转：``waiting``（排队中）→ ``ready``（已到书）→ ``fulfilled``（已借出）；
任意未结束状态可 ``cancelled``（读者 / 馆员取消）或 ``expired``（过期未取）。
"""

from __future__ import annotations

from typing import Any, Mapping

from library import db

# 未结束的状态集合：唯一索引与「是否还能取消」都基于它
OPEN_STATUSES = ("waiting", "ready")

_SELECT = """
SELECT r.id, r.book_id, r.member_id, r.status, r.ready_at, r.notified_at,
       r.closed_at, r.note, r.created_at, r.updated_at,
       b.isbn        AS book_isbn,
       b.barcode     AS book_barcode,
       b.title       AS book_title,
       b.author      AS book_author,
       b.category    AS book_category,
       b.location    AS book_location,
       b.total_copies,
       b.available_copies,
       m.card_no     AS member_card,
       m.name        AS member_name,
       m.phone       AS member_phone,
       m.status      AS member_status,
       m.card_lost   AS member_card_lost
  FROM reservations r
  JOIN books   b ON b.id = r.book_id
  JOIN members m ON m.id = r.member_id
"""


def get(res_id: int) -> dict[str, Any] | None:
    return db.query_one(_SELECT + " WHERE r.id = ?", [res_id])


def raw(res_id: int) -> dict[str, Any] | None:
    return db.query_one("SELECT * FROM reservations WHERE id = ?", [res_id])


def find_open(book_id: int, member_id: int) -> dict[str, Any] | None:
    """同一读者对同一本书的未结束预约（数据库有唯一索引兜底）。"""
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    return db.query_one(
        f"SELECT * FROM reservations WHERE book_id = ? AND member_id = ? "
        f"  AND status IN ({placeholders}) LIMIT 1",
        [book_id, member_id, *OPEN_STATUSES],
    )


def waiting_ahead(book_id: int, res_id: int) -> int:
    """该预约前面的排队人数（同一本书、状态 waiting、id 更小者）。"""
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM reservations "
            " WHERE book_id = ? AND status = 'waiting' AND id < ?",
            [book_id, res_id],
        )
        or 0
    )


def next_waiting(book_id: int) -> dict[str, Any] | None:
    """该书最早的一条排队预约（到书时用来唤醒队列）。"""
    return db.query_one(
        "SELECT * FROM reservations WHERE book_id = ? AND status = 'waiting' ORDER BY id LIMIT 1",
        [book_id],
    )


def open_count_for_book(book_id: int) -> int:
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    return int(
        db.query_scalar(
            f"SELECT COUNT(*) FROM reservations WHERE book_id = ? AND status IN ({placeholders})",
            [book_id, *OPEN_STATUSES],
        )
        or 0
    )


def _member_where(member_id: int, status: str) -> tuple[str, list[Any]]:
    clauses = ["r.member_id = ?"]
    params: list[Any] = [member_id]
    status = (status or "").strip()
    if status == "open":
        placeholders = ",".join("?" for _ in OPEN_STATUSES)
        clauses.append(f"r.status IN ({placeholders})")
        params.extend(OPEN_STATUSES)
    elif status and status != "all":
        clauses.append("r.status = ?")
        params.append(status)
    return " WHERE " + " AND ".join(clauses), params


def count_for_member(member_id: int, status: str = "") -> int:
    where, params = _member_where(member_id, status)
    return int(db.query_scalar(f"SELECT COUNT(*) FROM reservations r{where}", params) or 0)


def list_for_member(
    member_id: int, status: str, *, limit: int, offset: int
) -> list[dict[str, Any]]:
    where, params = _member_where(member_id, status)
    sql = _SELECT + where + " ORDER BY r.id DESC LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def count_all(filters: Mapping[str, Any]) -> int:
    where, params = _admin_where(filters)
    # 关键字过滤要命中 books / members 的列，所以计数也必须带上同样的 JOIN；
    # 少了 JOIN 时只要 keyword 非空就会报「no such column: b.title」。
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM reservations r"
            "  JOIN books   b ON b.id = r.book_id"
            "  JOIN members m ON m.id = r.member_id" + where,
            params,
        )
        or 0
    )


def list_all(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _admin_where(filters)
    sql = _SELECT + where + " ORDER BY (r.status = 'waiting') DESC, r.id ASC LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def _admin_where(filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    status = (filters.get("status") or "").strip()
    if status:
        clauses.append("r.status = ?")
        params.append(status)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(b.title LIKE ? OR b.isbn LIKE ? OR b.barcode LIKE ? OR m.name LIKE ? OR m.card_no LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def mark_ready(res_id: int) -> int:
    """waiting → ready（到书）。返回命中行数。"""
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE reservations SET status = 'ready', "
            "       ready_at = datetime('now', 'localtime'), "
            "       notified_at = datetime('now', 'localtime'), "
            "       updated_at = datetime('now', 'localtime') "
            " WHERE id = ? AND status = 'waiting'",
            [res_id],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def mark_fulfilled(res_id: int) -> int:
    """ready / waiting → fulfilled（已借出）。返回命中行数。"""
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE reservations SET status = 'fulfilled', "
            "       closed_at = datetime('now', 'localtime'), "
            "       updated_at = datetime('now', 'localtime') "
            f" WHERE id = ? AND status IN ({placeholders})",
            [res_id, *OPEN_STATUSES],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def close(res_id: int, status: str) -> int:
    """把未结束的预约置为 cancelled / expired。返回命中行数。"""
    with db.transaction() as conn:
        changed = close_with(conn, res_id, status)
    return changed


# ---------------------------------------------------------------- 事务内使用
# 下面几个函数接收**调用方的事务连接**，自己不开事务。
# 理由：`db.execute` / `db.transaction` 会自行提交，在外层 `BEGIN IMMEDIATE` 里
# 调用会中途结束外层事务，把「关旧单 + 开新单」这类原子语义打散。


def close_with(conn, res_id: int, status: str) -> int:
    placeholders = ",".join("?" for _ in OPEN_STATUSES)
    cur = conn.execute(
        "UPDATE reservations SET status = ?, "
        "       closed_at = datetime('now', 'localtime'), "
        "       updated_at = datetime('now', 'localtime') "
        f" WHERE id = ? AND status IN ({placeholders})",
        [status, res_id, *OPEN_STATUSES],
    )
    changed = max(0, int(cur.rowcount or 0))
    cur.close()
    return changed


def insert_with(conn, *, book_id: int, member_id: int, note: str = "") -> int:
    cur = conn.execute(
        "INSERT INTO reservations (book_id, member_id, status, note) VALUES (?, ?, 'waiting', ?)",
        [book_id, member_id, note],
    )
    new_id = int(cur.lastrowid or 0)
    cur.close()
    return new_id


def count_status(book_id: int, status: str) -> int:
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM reservations WHERE book_id = ? AND status = ?",
            [book_id, status],
        )
        or 0
    )


def list_expired_ready(hold_days: int) -> list[dict[str, Any]]:
    """已过取书保留期、仍未办理借出的「已到书」预约。"""
    return db.query_all(
        _SELECT + " WHERE r.status = 'ready' AND r.ready_at IS NOT NULL"
        "   AND datetime(r.ready_at, ?) < datetime('now', 'localtime')"
        " ORDER BY r.id",
        [f"+{int(hold_days)} days"],
    )
