"""读者互动数据访问层：收藏（我的书架）、图书评价、荐购登记、资料变更申请。

四张表都属于「读者自己产生的数据」，因此所有按读者的查询都把 ``member_id``
作为第一个过滤条件下推到 SQL——越权在仓储层就不成立，不靠上层记得过滤。

⚠️ 计数与列表必须**带同样的 JOIN**。这是本项目踩过的坑：`count` 用了
``FROM 主表 {where}`` 而 where 里引用了关联表的列，只要关键字非空就会
``no such column: b.title``。下面每一处 count 都刻意与对应 list 用同一个
``_from`` 片段拼出来，避免两套口径。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from library import db

# ================================================================ 我的书架（收藏）

_FAV_SELECT = """
SELECT f.id, f.member_id, f.book_id, f.note, f.created_at,
       b.isbn        AS book_isbn,
       b.barcode     AS book_barcode,
       b.title       AS book_title,
       b.author      AS book_author,
       b.publisher   AS book_publisher,
       b.category    AS book_category,
       b.location    AS book_location,
       b.total_copies,
       b.available_copies,
       (SELECT AVG(r.rating) FROM book_reviews r
         WHERE r.book_id = b.id AND r.status = 'published') AS rating_avg,
       (SELECT COUNT(*) FROM book_reviews r
         WHERE r.book_id = b.id AND r.status = 'published') AS rating_count
  FROM book_favorites f
  JOIN books b ON b.id = f.book_id
"""

_FAV_SORT_COLUMNS = {
    "id": "f.id",
    "title": "b.title COLLATE NOCASE",
    "created_at": "f.created_at",
}
_FAV_DEFAULT_ORDER = "f.id DESC"


def _fav_where(member_id: int, keyword: str) -> tuple[str, list[Any]]:
    clauses = ["f.member_id = ?"]
    params: list[Any] = [member_id]
    key = (keyword or "").strip()
    if key:
        clauses.append(
            "(b.title LIKE ? OR b.author LIKE ? OR b.isbn LIKE ? OR b.barcode LIKE ? OR f.note LIKE ?)"
        )
        like = f"%{key}%"
        params.extend([like, like, like, like, like])
    return " WHERE " + " AND ".join(clauses), params


def count_favorites(member_id: int, keyword: str = "") -> int:
    where, params = _fav_where(member_id, keyword)
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM book_favorites f JOIN books b ON b.id = f.book_id" + where, params
        )
        or 0
    )


def list_favorites(
    member_id: int,
    *,
    keyword: str = "",
    sort: str = "",
    order: str = "",
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    where, params = _fav_where(member_id, keyword)
    sort_clause = db.order_clause(
        {"sort": sort, "order": order}, _FAV_SORT_COLUMNS, _FAV_DEFAULT_ORDER
    )
    sql = _FAV_SELECT + where + f" ORDER BY {sort_clause} LIMIT ? OFFSET ?"
    return db.query_all(sql, [*params, limit, offset])


def get_favorite(favorite_id: int) -> dict[str, Any] | None:
    return db.query_one(_FAV_SELECT + " WHERE f.id = ?", [favorite_id])


def find_favorite(member_id: int, book_id: int) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM book_favorites WHERE member_id = ? AND book_id = ?", [member_id, book_id]
    )


def insert_favorite(member_id: int, book_id: int, note: str = "") -> int:
    return db.execute(
        "INSERT INTO book_favorites (member_id, book_id, note) VALUES (?, ?, ?)",
        [member_id, book_id, note],
    )


def update_favorite_note(member_id: int, favorite_id: int, note: str) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE book_favorites SET note = ? WHERE id = ? AND member_id = ?",
            [note, favorite_id, member_id],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def delete_favorite(member_id: int, favorite_id: int) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "DELETE FROM book_favorites WHERE id = ? AND member_id = ?", [favorite_id, member_id]
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def favorite_book_ids(member_id: int) -> set[int]:
    rows = db.query_all("SELECT book_id FROM book_favorites WHERE member_id = ?", [member_id])
    return {int(row["book_id"]) for row in rows}


# ================================================================ 图书评价

_REVIEW_SELECT = """
SELECT r.id, r.book_id, r.member_id, r.rating, r.content, r.status, r.created_at, r.updated_at,
       b.isbn  AS book_isbn,
       b.title AS book_title,
       b.author AS book_author,
       m.name  AS member_name,
       m.card_no AS member_card
  FROM book_reviews r
  JOIN books   b ON b.id = r.book_id
  JOIN members m ON m.id = r.member_id
"""

def _review_where(filters: Mapping[str, Any], *, alias_params: bool = True) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    member_id = filters.get("member_id")
    if member_id not in (None, ""):
        clauses.append("r.member_id = ?")
        params.append(int(member_id))
    book_id = filters.get("book_id")
    if book_id not in (None, ""):
        clauses.append("r.book_id = ?")
        params.append(int(book_id))
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(b.title LIKE ? OR b.author LIKE ? OR r.content LIKE ? OR m.name LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    status = (filters.get("status") or "").strip()
    if status and status != "all":
        clauses.append("r.status = ?")
        params.append(status)
    rating = filters.get("rating")
    if rating not in (None, "", "all"):
        clauses.append("r.rating = ?")
        params.append(int(rating))
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


_REVIEW_FROM = " FROM book_reviews r JOIN books b ON b.id = r.book_id JOIN members m ON m.id = r.member_id"


def count_reviews(filters: Mapping[str, Any]) -> int:
    where, params = _review_where(filters)
    return int(db.query_scalar(f"SELECT COUNT(*){_REVIEW_FROM}{where}", params) or 0)


def list_reviews(filters: Mapping[str, Any], *, limit: int, offset: int) -> list[dict[str, Any]]:
    where, params = _review_where(filters)
    order = db.order_clause(
        filters,
        {"id": "r.id", "rating": "r.rating", "created_at": "r.created_at"},
        "r.id DESC",
    )
    return db.query_all(_REVIEW_SELECT + where + f" ORDER BY {order} LIMIT ? OFFSET ?", [*params, limit, offset])


def get_review(review_id: int) -> dict[str, Any] | None:
    return db.query_one(_REVIEW_SELECT + " WHERE r.id = ?", [review_id])


def find_review(member_id: int, book_id: int) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM book_reviews WHERE member_id = ? AND book_id = ?", [member_id, book_id]
    )


def upsert_review(book_id: int, member_id: int, rating: int, content: str) -> int:
    """一人一书一条：已存在则覆盖（并保留原审核状态置回发布）。"""
    return db.execute(
        """
        INSERT INTO book_reviews (book_id, member_id, rating, content)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (member_id, book_id) DO UPDATE SET
            rating = excluded.rating,
            content = excluded.content,
            status = 'published',
            updated_at = datetime('now', 'localtime')
        """,
        [book_id, member_id, rating, content],
    )


def delete_review(member_id: int, review_id: int) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "DELETE FROM book_reviews WHERE id = ? AND member_id = ?", [review_id, member_id]
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def set_review_status(review_id: int, status: str) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE book_reviews SET status = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
            [status, review_id],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def list_published_reviews(book_id: int, *, limit: int, offset: int) -> list[dict[str, Any]]:
    return db.query_all(
        _REVIEW_SELECT
        + " WHERE r.book_id = ? AND r.status = 'published' ORDER BY r.updated_at DESC, r.id DESC"
        " LIMIT ? OFFSET ?",
        [book_id, limit, offset],
    )


def count_published_reviews(book_id: int) -> int:
    return int(
        db.query_scalar(
            "SELECT COUNT(*) FROM book_reviews WHERE book_id = ? AND status = 'published'", [book_id]
        )
        or 0
    )


def rating_summary(book_id: int) -> dict[str, Any]:
    """评分聚合：平均分、条数与 1–5 星分布。"""
    row = db.query_one(
        "SELECT AVG(rating) AS avg_rating, COUNT(*) AS total FROM book_reviews "
        " WHERE book_id = ? AND status = 'published'",
        [book_id],
    ) or {}
    buckets = db.query_all(
        "SELECT rating, COUNT(*) AS total FROM book_reviews "
        " WHERE book_id = ? AND status = 'published' GROUP BY rating",
        [book_id],
    )
    distribution = {str(star): 0 for star in range(1, 6)}
    for bucket in buckets:
        distribution[str(int(bucket["rating"]))] = int(bucket["total"])
    total = int(row.get("total") or 0)
    return {
        "avg": round(float(row["avg_rating"]), 1) if row.get("avg_rating") is not None else 0.0,
        "total": total,
        "distribution": distribution,
    }


def rating_map(book_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """批量取评分聚合（门户检索 / 推荐书单渲染星级用），避免 N+1 查询。"""
    ids = [int(b) for b in book_ids]
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    rows = db.query_all(
        f"SELECT book_id, AVG(rating) AS avg_rating, COUNT(*) AS total FROM book_reviews "
        f" WHERE status = 'published' AND book_id IN ({placeholders}) GROUP BY book_id",
        ids,
    )
    return {
        int(row["book_id"]): {
            "avg": round(float(row["avg_rating"]), 1) if row.get("avg_rating") is not None else 0.0,
            "total": int(row["total"] or 0),
        }
        for row in rows
    }


def reviewers_total(book_id: int) -> int:
    return int(
        db.query_scalar("SELECT COUNT(*) FROM book_reviews WHERE book_id = ?", [book_id]) or 0
    )


# ================================================================ 荐购登记

_REQUEST_SELECT = """
SELECT r.id, r.member_id, r.title, r.author, r.isbn, r.publisher, r.reason, r.status,
       r.reply, r.reviewed_by, r.reviewed_at, r.created_at, r.updated_at,
       m.name    AS member_name,
       m.card_no AS member_card
  FROM book_requests r
  JOIN members m ON m.id = r.member_id
"""

_REQ_FROM = " FROM book_requests r JOIN members m ON m.id = r.member_id"


def _book_request_where(member_id: int | None, filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if member_id is not None:
        clauses.append("r.member_id = ?")
        params.append(int(member_id))
    status = (filters.get("status") or "").strip()
    if status and status != "all":
        clauses.append("r.status = ?")
        params.append(status)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append(
            "(r.title LIKE ? OR r.author LIKE ? OR r.isbn LIKE ? OR r.publisher LIKE ?"
            " OR m.name LIKE ? OR m.card_no LIKE ?)"
        )
        like = f"%{keyword}%"
        params.extend([like, like, like, like, like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def count_book_requests(member_id: int | None, filters: Mapping[str, Any]) -> int:
    where, params = _book_request_where(member_id, filters)
    return int(db.query_scalar(f"SELECT COUNT(*){_REQ_FROM}{where}", params) or 0)


def list_book_requests(
    member_id: int | None, filters: Mapping[str, Any], *, limit: int, offset: int
) -> list[dict[str, Any]]:
    where, params = _book_request_where(member_id, filters)
    return db.query_all(_REQUEST_SELECT + where + " ORDER BY r.id DESC LIMIT ? OFFSET ?", [*params, limit, offset])


def get_book_request(request_id: int) -> dict[str, Any] | None:
    return db.query_one(_REQUEST_SELECT + " WHERE r.id = ?", [request_id])


def insert_book_request(member_id: int, data: Mapping[str, Any]) -> int:
    return db.execute(
        "INSERT INTO book_requests (member_id, title, author, isbn, publisher, reason) "
        "VALUES (:member_id, :title, :author, :isbn, :publisher, :reason)",
        {**dict(data), "member_id": member_id},
    )


def review_book_request(request_id: int, status: str, reply: str, operator: str) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE book_requests SET status = ?, reply = ?, reviewed_by = ?, "
            "       reviewed_at = datetime('now', 'localtime'), "
            "       updated_at = datetime('now', 'localtime') "
            " WHERE id = ? AND status = 'pending'",
            [status, reply, operator, request_id],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def book_request_counts() -> dict[str, int]:
    rows = db.query_all("SELECT status, COUNT(*) AS total FROM book_requests GROUP BY status")
    return {str(row["status"]): int(row["total"]) for row in rows}


# ================================================================ 资料变更申请

_PROFILE_SELECT = """
SELECT p.id, p.member_id, p.field, p.old_value, p.new_value, p.reason, p.status,
       p.reply, p.reviewed_by, p.reviewed_at, p.created_at, p.updated_at,
       m.name    AS member_name,
       m.card_no AS member_card
  FROM profile_requests p
  JOIN members m ON m.id = p.member_id
"""

_PR_FROM = " FROM profile_requests p JOIN members m ON m.id = p.member_id"


def _profile_request_where(member_id: int | None, filters: Mapping[str, Any]) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if member_id is not None:
        clauses.append("p.member_id = ?")
        params.append(int(member_id))
    status = (filters.get("status") or "").strip()
    if status and status != "all":
        clauses.append("p.status = ?")
        params.append(status)
    keyword = (filters.get("keyword") or "").strip()
    if keyword:
        clauses.append("(m.name LIKE ? OR m.card_no LIKE ? OR p.new_value LIKE ? OR p.reason LIKE ?)")
        like = f"%{keyword}%"
        params.extend([like, like, like, like])
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def count_profile_requests(member_id: int | None, filters: Mapping[str, Any]) -> int:
    where, params = _profile_request_where(member_id, filters)
    return int(db.query_scalar(f"SELECT COUNT(*){_PR_FROM}{where}", params) or 0)


def list_profile_requests(
    member_id: int | None, filters: Mapping[str, Any], *, limit: int, offset: int
) -> list[dict[str, Any]]:
    where, params = _profile_request_where(member_id, filters)
    return db.query_all(_PROFILE_SELECT + where + " ORDER BY p.id DESC LIMIT ? OFFSET ?", [*params, limit, offset])


def get_profile_request(request_id: int) -> dict[str, Any] | None:
    return db.query_one(_PROFILE_SELECT + " WHERE p.id = ?", [request_id])


def find_pending_profile_request(member_id: int, field: str) -> dict[str, Any] | None:
    return db.query_one(
        "SELECT * FROM profile_requests WHERE member_id = ? AND field = ? AND status = 'pending' LIMIT 1",
        [member_id, field],
    )


def insert_profile_request(member_id: int, data: Mapping[str, Any]) -> int:
    return db.execute(
        "INSERT INTO profile_requests (member_id, field, old_value, new_value, reason) "
        "VALUES (:member_id, :field, :old_value, :new_value, :reason)",
        {**dict(data), "member_id": member_id},
    )


def review_profile_request(request_id: int, status: str, reply: str, operator: str) -> int:
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE profile_requests SET status = ?, reply = ?, reviewed_by = ?, "
            "       reviewed_at = datetime('now', 'localtime'), "
            "       updated_at = datetime('now', 'localtime') "
            " WHERE id = ? AND status = 'pending'",
            [status, reply, operator, request_id],
        )
        changed = max(0, int(cur.rowcount or 0))
        cur.close()
    return changed


def profile_request_counts() -> dict[str, int]:
    rows = db.query_all("SELECT status, COUNT(*) AS total FROM profile_requests GROUP BY status")
    return {str(row["status"]): int(row["total"]) for row in rows}


# ================================================================ 待评价

def list_reviewable_books(member_id: int, *, limit: int) -> list[dict[str, Any]]:
    """读者借过（已归还）但还没评价过的书——「待评价」清单。

    只认**已归还**的借阅：书还在手上就让人打分，等于鼓励没读完就评。
    """
    return db.query_all(
        """
        SELECT b.id AS book_id, b.title AS book_title, b.author AS book_author,
               b.isbn AS book_isbn, b.barcode AS book_barcode,
               MAX(l.returned_at) AS last_returned_at
          FROM loans l
          JOIN books b ON b.id = l.book_id
         WHERE l.member_id = ? AND l.returned_at IS NOT NULL
           AND NOT EXISTS (
                 SELECT 1 FROM book_reviews r
                  WHERE r.book_id = b.id AND r.member_id = l.member_id
               )
         GROUP BY b.id
         ORDER BY last_returned_at DESC
         LIMIT ?
        """,
        [member_id, limit],
    )


def borrowed_book_ids(member_id: int, book_ids: Iterable[int]) -> set[int]:
    """这批书里哪些是该读者借过的（评价资格校验，一次查询判完）。"""
    ids = [int(b) for b in book_ids]
    if not ids:
        return set()
    placeholders = ",".join("?" for _ in ids)
    rows = db.query_all(
        f"SELECT DISTINCT book_id FROM loans "
        f" WHERE member_id = ? AND returned_at IS NOT NULL AND book_id IN ({placeholders})",
        [member_id, *ids],
    )
    return {int(row["book_id"]) for row in rows}
