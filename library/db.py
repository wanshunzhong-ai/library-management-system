"""数据库连接、Schema 迁移与查询辅助。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from config import get_config
from library.barcode import generate

logger = logging.getLogger(__name__)

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    isbn             TEXT    NOT NULL UNIQUE,
    barcode          TEXT    NOT NULL DEFAULT '',
    title            TEXT    NOT NULL,
    author           TEXT    NOT NULL DEFAULT '',
    publisher        TEXT    NOT NULL DEFAULT '',
    category         TEXT    NOT NULL DEFAULT '未分类',
    publish_year     INTEGER,
    location         TEXT    NOT NULL DEFAULT '',
    description      TEXT    NOT NULL DEFAULT '',
    total_copies     INTEGER NOT NULL DEFAULT 1 CHECK (total_copies >= 0),
    -- 可借数不得为负、也不得超过总册数（后者是并发还书重复回补的兜底）
    available_copies INTEGER NOT NULL DEFAULT 1
                     CHECK (available_copies >= 0 AND available_copies <= total_copies),
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    card_no       TEXT    NOT NULL UNIQUE,
    name          TEXT    NOT NULL,
    phone         TEXT    NOT NULL DEFAULT '',
    email         TEXT    NOT NULL DEFAULT '',
    member_type   TEXT    NOT NULL DEFAULT '普通',
    status        TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'frozen')),
    max_borrow    INTEGER NOT NULL DEFAULT 5 CHECK (max_borrow >= 1),
    password_hash TEXT    NOT NULL DEFAULT '',
    last_login_at TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE IF NOT EXISTS loans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id     INTEGER NOT NULL REFERENCES books(id) ON DELETE RESTRICT,
    member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE RESTRICT,
    borrowed_at TEXT    NOT NULL,
    due_at      TEXT    NOT NULL,
    returned_at TEXT,
    renew_count INTEGER NOT NULL DEFAULT 0,
    note        TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,
    display_name  TEXT    NOT NULL DEFAULT '',
    role          TEXT    NOT NULL DEFAULT 'admin' CHECK (role IN ('admin', 'librarian')),
    is_active     INTEGER NOT NULL DEFAULT 1,
    last_login_at TEXT,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_books_title      ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_category   ON books(category);
CREATE INDEX IF NOT EXISTS idx_books_author     ON books(author);
CREATE INDEX IF NOT EXISTS idx_members_name     ON members(name);
CREATE INDEX IF NOT EXISTS idx_members_card     ON members(card_no);
CREATE INDEX IF NOT EXISTS idx_members_phone    ON members(phone);
CREATE INDEX IF NOT EXISTS idx_loans_book       ON loans(book_id);
CREATE INDEX IF NOT EXISTS idx_loans_member     ON loans(member_id);
CREATE INDEX IF NOT EXISTS idx_loans_open       ON loans(returned_at, due_at);
"""


def _connect() -> sqlite3.Connection:
    cfg = get_config()
    db_path = Path(cfg.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(cfg.db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def get_conn() -> sqlite3.Connection:
    """请求内复用同一个连接；非 Web 上下文（测试/脚本）退化为线程级连接。"""
    try:
        from flask import g, has_app_context
    except ImportError:  # pragma: no cover - Flask 一定存在
        has_app_context = lambda: False  # noqa: E731
        g = None  # type: ignore[assignment]

    if g is not None and has_app_context():
        conn = getattr(g, "_library_conn", None)
        if conn is None:
            conn = _connect()
            g._library_conn = conn
        return conn

    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


def close_conn(exc: BaseException | None = None) -> None:
    """Flask teardown：请求结束关闭连接。"""
    try:
        from flask import g, has_app_context
    except ImportError:  # pragma: no cover
        return
    if not has_app_context():
        return
    conn = getattr(g, "_library_conn", None)
    if conn is not None:
        conn.close()
        g._library_conn = None


@contextmanager
def transaction(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """显式事务：成功提交，异常回滚。用于借书/还书这类多表联动写操作。

    ``immediate=True`` 使用 ``BEGIN IMMEDIATE``：事务开始即取写锁，把并发写请求
    串行化。默认的 deferred 事务只有在首次写入时才取锁，事务内的 SELECT 仍读旧快照，
    「先查后写」的校验（借阅上限、重复借、续借次数）会被并发绕过 —— 凡是有
    「读-判断-写」语义的借阅写路径都必须用 immediate。
    """
    conn = get_conn()
    if immediate and not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query_all(sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    cur = get_conn().execute(sql, params)
    try:
        return [dict(row) for row in cur.fetchall()]
    finally:
        cur.close()


def query_one(sql: str, params: Sequence[Any] = ()) -> dict[str, Any] | None:
    cur = get_conn().execute(sql, params)
    try:
        row = cur.fetchone()
        return dict(row) if row is not None else None
    finally:
        cur.close()


def query_scalar(sql: str, params: Sequence[Any] = ()) -> Any:
    cur = get_conn().execute(sql, params)
    try:
        row = cur.fetchone()
        return row[0] if row is not None else None
    finally:
        cur.close()


def execute(sql: str, params: Sequence[Any] = ()) -> int:
    """执行写语句，返回 lastrowid。"""
    with transaction() as conn:
        cur = conn.execute(sql, params)
        last_id = cur.lastrowid
        cur.close()
    return int(last_id or 0)


def executemany(sql: str, seq: Iterable[Sequence[Any]]) -> None:
    with transaction() as conn:
        conn.executemany(sql, seq)


def order_clause(
    filters: Mapping[str, Any], columns: Mapping[str, str], default: str
) -> str:
    """把 ``sort`` / ``order`` 查询参数解析成安全的 ORDER BY 片段。

    ``columns`` 是「对外排序键 → 真实列表达式」的白名单，未命中或方向非法时
    回落到 ``default``。拼进 SQL 的字符串只来自代码里的常量映射，
    用户输入仅用于查字典与二选一，因此不存在拼接注入面。
    """
    key = str(filters.get("sort") or "").strip().lower()
    column = columns.get(key)
    if not column:
        return default
    direction = "ASC" if str(filters.get("order") or "").strip().lower() == "asc" else "DESC"
    return f"{column} {direction}"


def init_db() -> None:
    """建表 + 建索引 + 增量迁移（幂等）。"""
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    logger.info("数据库已就绪", extra={"extra_fields": {"db_path": get_config().db_path}})


# 老库升级用的增量迁移：新库由 SCHEMA 直接建全，这里只补历史库缺的列。
# 每条迁移都是幂等的（先查 PRAGMA 再决定加不加），可安全重复执行。
_COLUMN_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    ("members", "password_hash", "ALTER TABLE members ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''"),
    ("members", "last_login_at", "ALTER TABLE members ADD COLUMN last_login_at TEXT"),
    ("books", "barcode", "ALTER TABLE books ADD COLUMN barcode TEXT NOT NULL DEFAULT ''"),
    ("books", "description", "ALTER TABLE books ADD COLUMN description TEXT NOT NULL DEFAULT ''"),
)

# 条码唯一索引必须晚于「列创建 + 存量回填」执行，因此不放进 SCHEMA，
# 由 _migrate 在回填之后幂等创建。
_BARCODE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_books_barcode ON books(barcode) WHERE barcode <> ''"
)

# 同一读者对同一本书只能有一条「未归还」记录。
# 应用层已有校验，这里是数据库层兜底，防止并发写入产生重复在借。
_LOANS_OPEN_UNIQUE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_loans_open_unique "
    "ON loans(book_id, member_id) WHERE returned_at IS NULL"
)

# 手机号与证号一样是登录账号，必须全局唯一（空串代表未填，不参与约束）。
# 应用层校验之外再加数据库兜底，避免并发建档写入重复手机号后身份归属不明。
_MEMBERS_PHONE_UNIQUE_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_members_phone_unique "
    "ON members(phone) WHERE phone <> ''"
)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    cur = conn.execute(f"PRAGMA table_info({table})")
    try:
        return {str(row[1]) for row in cur.fetchall()}
    finally:
        cur.close()


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, ddl in _COLUMN_MIGRATIONS:
        if column in _table_columns(conn, table):
            continue
        conn.execute(ddl)
        logger.info(
            "数据库迁移完成",
            extra={"extra_fields": {"table": table, "added_column": column}},
        )
    _backfill_barcodes(conn)
    conn.execute(_BARCODE_INDEX_SQL)
    _repair_inventory(conn)
    _create_open_loan_index(conn)
    _create_member_phone_index(conn)


def _repair_inventory(conn: sqlite3.Connection) -> None:
    """把越界的可借数夹回合法区间（幂等）。

    历史版本在并发还书时可能重复回补库存，导致 ``available_copies > total_copies``；
    老库无法通过 ALTER 补上 CHECK 约束，因此这里做一次性数据修复。
    """
    over = conn.execute(
        "UPDATE books SET available_copies = total_copies WHERE available_copies > total_copies"
    )
    under = conn.execute("UPDATE books SET available_copies = 0 WHERE available_copies < 0")
    fixed = int(over.rowcount or 0) + int(under.rowcount or 0)
    if fixed:
        logger.warning(
            "库存越界数据已修复",
            extra={"extra_fields": {"fixed_rows": fixed}},
        )


def _create_open_loan_index(conn: sqlite3.Connection) -> None:
    """创建「同一读者同一本书只能有一条未归还记录」的唯一索引。

    老库若已存在重复在借数据，建索引会失败：此时记警告并跳过，
    不自动改写借阅记录（由 check_consistency() 报告，交人工处理）。
    """
    try:
        conn.execute(_LOANS_OPEN_UNIQUE_SQL)
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "存在重复在借记录，未能创建唯一索引 idx_loans_open_unique：%s；"
            "请先用 check_consistency() 排查",
            exc,
        )


def _create_member_phone_index(conn: sqlite3.Connection) -> None:
    """创建手机号唯一索引（空串不参与）。

    老库若已有重复手机号，建索引会失败：记警告并跳过，不自动清空任何人的
    联系方式（那是丢数据），由 check_consistency() 报告后交人工处理。
    """
    try:
        conn.execute(_MEMBERS_PHONE_UNIQUE_SQL)
    except sqlite3.IntegrityError as exc:
        logger.warning(
            "存在重复手机号，未能创建唯一索引 idx_members_phone_unique：%s；"
            "请先用 check_consistency() 排查",
            exc,
        )


def check_consistency() -> dict[str, Any]:
    """数据一致性自检：库存越界、重复在借、库存总量与在借数是否对得上。

    返回 ``{"ok": bool, "issues": [...]}``，可用于运维巡检与测试断言。
    """
    issues: list[dict[str, Any]] = []

    for row in query_all(
        "SELECT id, title, total_copies, available_copies FROM books "
        " WHERE available_copies < 0 OR available_copies > total_copies"
    ):
        issues.append({"type": "inventory_out_of_range", **row})

    for row in query_all(
        "SELECT book_id, member_id, COUNT(*) AS duplicates FROM loans "
        " WHERE returned_at IS NULL GROUP BY book_id, member_id HAVING duplicates > 1"
    ):
        issues.append({"type": "duplicate_open_loan", **row})

    for row in query_all(
        "SELECT phone, COUNT(*) AS duplicates FROM members "
        " WHERE phone <> '' GROUP BY phone HAVING duplicates > 1"
    ):
        issues.append({"type": "duplicate_member_phone", **row})

    for row in query_all(
        "SELECT m.id AS member_id, m.card_no, m.phone, x.id AS other_member_id "
        "  FROM members m JOIN members x ON x.card_no = m.phone AND x.id <> m.id "
        " WHERE m.phone <> ''"
    ):
        issues.append({"type": "phone_shadows_card_no", **row})

    for row in query_all(
        "SELECT m.id AS member_id, m.card_no, m.phone, a.username "
        "  FROM members m JOIN admins a "
        "    ON a.username = m.card_no OR (m.phone <> '' AND a.username = m.phone)"
    ):
        issues.append({"type": "member_identifier_shadows_admin", **row})

    summary = query_one(
        "SELECT (SELECT COALESCE(SUM(total_copies), 0)     FROM books) AS total_copies,"
        "       (SELECT COALESCE(SUM(available_copies), 0) FROM books) AS available_copies,"
        "       (SELECT COUNT(*) FROM loans WHERE returned_at IS NULL) AS open_loans"
    ) or {}
    total_copies = int(summary.get("total_copies") or 0)
    available_copies = int(summary.get("available_copies") or 0)
    open_loans = int(summary.get("open_loans") or 0)
    if available_copies != total_copies - open_loans:
        issues.append(
            {
                "type": "inventory_mismatch",
                "available_copies": available_copies,
                "expected": total_copies - open_loans,
            }
        )

    return {
        "ok": not issues,
        "issues": issues,
        "total_copies": total_copies,
        "available_copies": available_copies,
        "open_loans": open_loans,
    }


def repair_consistency() -> dict[str, Any]:
    """修复有明确正确答案的数据问题：同一读者同一本书的重复在借记录。

    保留最早的一条，把多余的标记为已归还（归还时间取其应还日期），并把因此
    多出来的那一册补回可借数——账实依旧对得上。只碰这一种情况，其余问题
    （例如重复手机号）一律留给人工判断，避免误改真实数据。

    返回 ``{"closed_loans": n, "before": ..., "after": ...}``。
    """
    before = check_consistency()
    closed = 0
    with transaction(immediate=True) as conn:
        groups = conn.execute(
            "SELECT book_id, member_id FROM loans "
            " WHERE returned_at IS NULL GROUP BY book_id, member_id HAVING COUNT(*) > 1"
        ).fetchall()
        for book_id, member_id in groups:
            duplicates = conn.execute(
                "SELECT id, due_at FROM loans WHERE book_id = ? AND member_id = ? "
                "  AND returned_at IS NULL ORDER BY id",
                [book_id, member_id],
            ).fetchall()
            for loan_id, due_at in duplicates[1:]:
                conn.execute(
                    "UPDATE loans SET returned_at = ? WHERE id = ? AND returned_at IS NULL",
                    [due_at or _now_str(), loan_id],
                )
                conn.execute(
                    "UPDATE books SET available_copies = MIN(total_copies, available_copies + 1), "
                    "       updated_at = datetime('now', 'localtime') WHERE id = ?",
                    [book_id],
                )
                closed += 1

    if closed:
        # 数据干净了，把当初建不上的唯一索引补上
        conn = get_conn()
        _create_open_loan_index(conn)
        conn.commit()
        logger.warning(
            "已修复重复在借记录",
            extra={"extra_fields": {"closed_loans": closed}},
        )
    after = check_consistency()
    return {"closed_loans": closed, "before": before, "after": after}


def _now_str() -> str:
    from library import timeutil

    return timeutil.to_str(timeutil.now())


def _backfill_barcodes(conn: sqlite3.Connection) -> None:
    """为缺条码的图书补生成馆藏条码号（幂等）。

    条码号默认取「BK + 7 位序号」，序号优先复用自己的图书 id；
    若该号已被手工占用则顺延到第一个未占用的序号。
    """
    rows = conn.execute("SELECT id FROM books WHERE barcode IS NULL OR barcode = '' ORDER BY id").fetchall()
    if not rows:
        return

    used = {
        str(row[0]).upper()
        for row in conn.execute("SELECT barcode FROM books WHERE barcode <> ''").fetchall()
    }
    seq = 1
    for row in rows:
        book_id = int(row[0])
        candidate = generate(book_id)
        if candidate in used:
            while generate(seq) in used:
                seq += 1
            candidate = generate(seq)
        used.add(candidate)
        conn.execute("UPDATE books SET barcode = ? WHERE id = ?", [candidate, book_id])

    logger.info(
        "数据库迁移完成",
        extra={"extra_fields": {"table": "books", "backfilled_barcodes": len(rows)}},
    )
