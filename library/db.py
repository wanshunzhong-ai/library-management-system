"""数据库连接、Schema 迁移与查询辅助。"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

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
    available_copies INTEGER NOT NULL DEFAULT 1 CHECK (available_copies >= 0),
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
def transaction() -> Iterator[sqlite3.Connection]:
    """显式事务：成功提交，异常回滚。用于借书/还书这类多表联动写操作。"""
    conn = get_conn()
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
