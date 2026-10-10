"""管理员账号数据访问层。"""

from __future__ import annotations

from typing import Any

from library import db

_COLUMNS = "id, username, password_hash, display_name, role, is_active, last_login_at, created_at"


def count() -> int:
    return int(db.query_scalar("SELECT COUNT(*) FROM admins") or 0)


def list_all() -> list[dict[str, Any]]:
    """全部管理员（启动自检默认口令用）。"""
    return db.query_all(f"SELECT {_COLUMNS} FROM admins ORDER BY id")


def get_by_username(username: str) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM admins WHERE username = ?", [username])


def exists_username(username: str) -> bool:
    """该用户名是否已被管理员工号占用（读者证号/手机号不能与之重名，否则登录会互相遮蔽）。"""
    return bool(db.query_scalar("SELECT 1 FROM admins WHERE username = ? LIMIT 1", [username]))


def get(admin_id: int) -> dict[str, Any] | None:
    return db.query_one(f"SELECT {_COLUMNS} FROM admins WHERE id = ?", [admin_id])


def create(username: str, password_hash: str, display_name: str, role: str = "admin") -> int:
    return db.execute(
        "INSERT INTO admins (username, password_hash, display_name, role) VALUES (?, ?, ?, ?)",
        [username, password_hash, display_name, role],
    )


def update_password(admin_id: int, password_hash: str) -> None:
    db.execute("UPDATE admins SET password_hash = ? WHERE id = ?", [password_hash, admin_id])


def touch_login(admin_id: int) -> None:
    db.execute("UPDATE admins SET last_login_at = datetime('now', 'localtime') WHERE id = ?", [admin_id])
